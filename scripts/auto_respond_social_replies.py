"""Unified inbound reply monitor for X and LinkedIn.

Answers people who replied to / commented on / mentioned Vishal on either
platform. This is the single owner of inbound social replies -- auto_reply_inbound.py
(X-only) duplicated this script's X half against a separate dedupe table and
double-replied to every tweet; its history is migrated in on first run.

Usage:
    python auto_respond_social_replies.py            # live, posts replies
    python auto_respond_social_replies.py --dry-run  # scan + report, posts nothing
"""

import os
import sys
import time
import json
import sqlite3
import hashlib
import argparse
from urllib.parse import unquote, urlparse, parse_qs

from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# Capture this run's full stdout and stderr to
# ~/.hermes/logs/pipelines/social_all_reply_inbound/ so a failed or silent run can be
# diagnosed afterwards instead of vanishing.
if __name__ == '__main__':
    # Only when run directly. Firing on import made every script that
    # imports this module log its own run under this pipeline's name.
    try:
        import pipeline_log as _plog
        _plog.start('social_all_reply_inbound')
    except Exception:
        pass

import social_voice as voice
import shared_dedupe as dedupe
from browser_lock import profile_lock, BrowserBusy

DB_PATH = os.path.expanduser('~/.hermes/data/x_growth.db')
AUTOMATION_PROFILE_DIR = os.path.expanduser('~/.hermes/data/chrome_automation_profile')

OWN_X_HANDLES = {'vishalpanwarr', ''}
OWN_LI_NAME = 'Vishal Panwar'

MAX_X_ACTIONS = 6
MAX_LI_POSTS = 8
MAX_LI_REPLIES_PER_POST = 2


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------

def init_tables():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('''
    CREATE TABLE IF NOT EXISTS social_replies_handled (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        platform TEXT NOT NULL, -- 'x' or 'linkedin'
        interaction_id TEXT UNIQUE NOT NULL,
        author TEXT,
        incoming_text TEXT,
        reply_generated TEXT,
        status TEXT DEFAULT 'posted',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    conn.commit()

    # Fold in the retired auto_reply_inbound.py history so we never re-reply to
    # a tweet it already answered. Its ids were bare tweet_ids; ours are x_<id>.
    try:
        cur.execute('''
        INSERT OR IGNORE INTO social_replies_handled
            (platform, interaction_id, author, incoming_text, reply_generated, status, created_at)
        SELECT 'x', 'x_' || tweet_id, author, tweet_text, reply_text, 'posted', created_at
        FROM inbound_replies
        ''')
        if cur.rowcount:
            print(f"Migrated {cur.rowcount} rows from inbound_replies.", file=sys.stderr)
        conn.commit()
    except sqlite3.OperationalError:
        pass  # inbound_replies never existed on this machine

    conn.close()


def stable_id(platform, *parts):
    """Dedupe key that survives across processes.

    The previous LinkedIn key used hash(), which Python randomises per process,
    so the same comment produced a different id on every run and dedupe never
    matched.
    """
    raw = '|'.join(p.strip() for p in parts if p)
    digest = hashlib.sha1(raw.encode('utf-8', 'replace')).hexdigest()[:20]
    return f"{platform}_{digest}"


def is_already_handled(interaction_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM social_replies_handled WHERE interaction_id = ?", (interaction_id,))
    row = cur.fetchone()
    conn.close()
    return row is not None


def record_handled(platform, interaction_id, author, incoming_text, reply_generated, status='posted'):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('''
    INSERT OR IGNORE INTO social_replies_handled
        (platform, interaction_id, author, incoming_text, reply_generated, status)
    VALUES (?, ?, ?, ?, ?, ?)
    ''', (platform, interaction_id, author, incoming_text, reply_generated, status))
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------
# playwright helpers
# --------------------------------------------------------------------------

def first_visible(scope, selector):
    """Return the first visible match, or None.

    Guards the .first.is_visible() pattern: calling is_visible() on .first of an
    empty locator raises rather than returning False, which previously killed
    the whole notification via the outer except.
    """
    try:
        loc = scope.locator(selector).first
        if loc.count() and loc.is_visible():
            return loc
    except Exception:
        pass
    return None


_EDITOR_SEL = ('div.ql-editor[contenteditable="true"], '
               'div.tiptap[contenteditable="true"], '
               'div[role="textbox"][contenteditable="true"]')


def reply_editor_for(page, comment):
    """The reply box belonging to THIS comment.

    Searching the page for the first visible editor grabs whichever one happens
    to come first in DOM order, which can be the post's own composer or another
    commenter's open reply box. The reply then lands under the wrong person.

    LinkedIn injects the reply editor as a descendant of the comment or as a
    sibling just after it, so mark the comment and its parent and search only
    inside that scope.
    """
    try:
        comment.evaluate(
            "n => { n.setAttribute('data-hermes-scope','1');"
            "       if (n.parentElement) n.parentElement.setAttribute('data-hermes-scope','1'); }")
    except Exception:
        return first_visible(page, _EDITOR_SEL)

    found = None
    try:
        scoped = page.locator(
            ', '.join(f'[data-hermes-scope] {sel.strip()}' for sel in _EDITOR_SEL.split(',')))
        for i in range(scoped.count()):
            e = scoped.nth(i)
            if e.is_visible():
                # Resolve to a concrete handle BEFORE the marker is stripped:
                # a locator is lazy and would stop matching once the attribute
                # it selects on is removed.
                found = e.element_handle()
                break
    except Exception:
        found = None

    try:
        page.evaluate("() => document.querySelectorAll('[data-hermes-scope]')"
                      ".forEach(n => n.removeAttribute('data-hermes-scope'))")
    except Exception:
        pass

    return found


def text_of(scope, selector, default=''):
    loc = first_visible(scope, selector)
    if loc is None:
        return default
    try:
        return loc.inner_text().strip()
    except Exception:
        return default


# --------------------------------------------------------------------------
# reply text
# --------------------------------------------------------------------------

HERMES_ENV = os.path.expanduser('~/.hermes/.env')
HERMES_CONFIG = os.path.expanduser('~/.hermes/config.yaml')

PERSONA = """You are drafting a social media reply AS Vishal Panwar.

About Vishal: backend / AI engineer. Go, Node.js, Python, FastAPI, Next.js,
Kubernetes, GCP. Portfolio https://vishalpanwar.in. Open-source project
multi-qr (https://github.com/vishalpanwar416/multi-qr, `npm i multi-qr`).
Currently open to Backend / AI Engineer roles.

Rules:
- Reply to the SUBSTANCE of what they said. Add one concrete, specific thought.
- First person, natural, peer-to-peer. No corporate filler, no "Great point!".
- No hashtags. At most one emoji, usually none.
- Never invent facts about Vishal's work history, employers, or metrics.
- Only mention the portfolio or repo if they actually asked for a link.
- Hard limit {limit} characters. One short paragraph.
- If no worthwhile reply exists (spam, hostile, or nothing to add), output
  exactly: SKIP"""


class _Skip:
    """Sentinel: the model judged this not worth replying to."""
    def __repr__(self):
        return '<SKIP>'


SKIP = _Skip()


def _load_env_key(name):
    val = os.environ.get(name)
    if val:
        return val.strip()
    try:
        with open(HERMES_ENV) as fh:
            for line in fh:
                line = line.strip()
                if line.startswith(f'{name}='):
                    return line.split('=', 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return None


def _configured_model(default='google/gemini-3.7-flash'):
    try:
        with open(HERMES_CONFIG) as fh:
            in_model = False
            for line in fh:
                if line.startswith('model:'):
                    in_model = True
                    continue
                if in_model:
                    if line.startswith((' ', '\t')):
                        if 'default:' in line:
                            return line.split('default:', 1)[1].strip()
                    else:
                        break
    except OSError:
        pass
    return default


def model_reply(author, text, platform, limit):
    """Draft a reply with the model Hermes is already configured to use.

    Returns the reply, or None to fall through to the keyword rules (missing
    key, API failure, or the model deciding there's nothing worth saying).
    """
    api_key = _load_env_key('OPENROUTER_API_KEY')
    if not api_key:
        return None

    import urllib.request

    payload = json.dumps({
        'model': _configured_model(),
        'provider': {'only': ['google-ai-studio']},
        # The configured model is a reasoning model and google-ai-studio rejects
        # disabling it ("Reasoning is mandatory for this endpoint"). At the old
        # 300-token cap it spent 268 tokens thinking and returned a reply cut off
        # mid-sentence, so: ask for the smallest reasoning budget allowed, drop
        # the traces from the response, and leave real headroom for the answer.
        'reasoning': {'effort': 'minimal', 'exclude': True},
        'max_tokens': 600,
        'temperature': 0.7,
        'messages': [
            {'role': 'system', 'content': PERSONA.format(limit=limit)},
            {'role': 'user', 'content':
                f"Platform: {platform}\nThey wrote (author: {author}):\n\n{text[:1500]}\n\n"
                f"Write Vishal's reply."},
        ],
    }).encode('utf-8')

    req = urllib.request.Request(
        'https://openrouter.ai/api/v1/chat/completions',
        data=payload,
        headers={'Authorization': f'Bearer {api_key}',
                 'Content-Type': 'application/json'},
    )

    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            body = json.loads(resp.read().decode('utf-8'))
        choice = body['choices'][0]
        # Never post a reply the model didn't finish -- a truncated response
        # ends mid-sentence, and posting that is worse than the canned fallback.
        if choice.get('finish_reason') == 'length':
            print("[model] response hit the token cap; using fallback", file=sys.stderr)
            return None
        reply = (choice['message'].get('content') or '').strip()
    except Exception as e:
        print(f"[model] call failed, falling back to keyword rules: {e}", file=sys.stderr)
        return None

    reply = ' '.join(reply.split()).strip('"').strip()
    if not reply:
        return None
    if reply.upper().startswith('SKIP'):
        # A deliberate "nothing worth saying" verdict -- distinct from an API
        # failure. Falling through to the keyword rules here meant spam and
        # hostile comments still got a cheerful canned answer.
        return SKIP
    if len(reply) > limit:
        cut = reply[:limit]
        reply = cut.rsplit('. ', 1)[0] + '.' if '. ' in cut else cut.rstrip()
    return reply


def generate_reply(author, text, platform):
    """Model-drafted reply, or None when this shouldn't be answered at all.

    Voice, length matching and the de-AI scrub all live in social_voice so that
    replies here, growth replies and posts sound like one person. Falls back to
    the keyword rules only when the call *failed*, not when the model declined.
    """
    drafted = voice.draft_reply(author, text, platform)
    if drafted is voice.SKIP:
        print(f"[voice] declined to reply to {author}", file=sys.stderr)
        return None
    if drafted:
        print(f"[voice] drafted {len(drafted)} chars for {author}", file=sys.stderr)
        return drafted
    # Even the canned fallback goes through the scrub, so a stray dash in the
    # hand written pools can't leak out either.
    return voice.humanize(generate_contextual_response(author, text))


def generate_contextual_response(author, text):
    """Merged rules from both the old unified script and auto_reply_inbound.py,
    so retiring the X-only job loses none of its response patterns.

    Now the fallback path behind generate_reply()."""
    t = text.lower()

    if any(q in t for q in ['have you applied', 'did you apply', 'apply yet']):
        return "Checking out the details and applying now! Thanks for sharing."

    if any(q in t for q in ['github', 'repo', 'link', 'npm', 'source', 'code', 'where to find']):
        if 'qr' in t or 'multi' in t or 'wifi' in t:
            return ("Here is the repo and npm package: https://github.com/vishalpanwar416/multi-qr "
                    "(`npm i multi-qr`). Feedback and PRs welcome!")
        return "You can check out my live projects and proof of work here: https://vishalpanwar.in"

    if any(q in t for q in ['portfolio?', 'resume?', 'cv?']):
        return "You can check out my portfolio and live projects here: https://vishalpanwar.in"

    if any(q in t for q in ['open to work', 'looking for a job', 'available for roles', 'hiring']):
        return ("Yes, actively looking for Backend / AI Engineer roles (Go, Node, K8s, GCP). "
                "Portfolio: https://vishalpanwar.in")

    if any(k in t for k in ['how did you', 'what stack', 'how do you']):
        if 'latency' in t or 'go' in t or 'p95' in t:
            return ("Mainly buffer reuse with sync.Pool, streaming JSON encoders, and bounded "
                    "worker pools to avoid GC spikes at high RPS.")
        if 'qr' in t or 'wifi' in t:
            return ("Built a custom codec that packs multiple action schemas into a compact "
                    "binary-safe string before feeding it into the standard QR matrix.")
        return ("Built using Go microservices and React/Next.js with low-latency caching on GCP. "
                "Happy to share more details!")

    if any(c in t for c in ['sounds fun', 'love that', 'awesome', 'amazing', 'great job',
                            'cool project', 'nice one', 'good work']):
        return "Appreciate it! Glad you found it interesting."

    if 'thanks' in t or 'thank you' in t:
        return "You're welcome! Let's connect."

    return "Thanks for the shoutout! Glad to connect."


# --------------------------------------------------------------------------
# X
# --------------------------------------------------------------------------

def collect_x_mentions(page):
    """Harvest (tweet_id, author, text) before clicking anything.

    Clicking navigates, which detaches every handle in a pre-navigation
    snapshot -- the bug that meant only the first notification could ever be
    processed. Collect plain data first, then navigate by URL.
    """
    page.goto("https://x.com/notifications/mentions", timeout=35000, wait_until="domcontentloaded")
    time.sleep(4)

    items = page.locator('article[data-testid="tweet"]').all()
    if not items:
        page.goto("https://x.com/notifications", timeout=35000, wait_until="domcontentloaded")
        time.sleep(4)
        items = page.locator('article[data-testid="tweet"]').all()

    print(f"[X] {len(items)} notification items on page.", file=sys.stderr)

    mentions = []
    for item in items:
        try:
            text = item.inner_text()
            if "@VishalPanwarr" not in text and "Replying to" not in text:
                continue

            tweet_url, author = "", ""
            for link in item.locator('a').all():
                href = link.get_attribute('href') or ''
                if '/status/' in href and not tweet_url:
                    tweet_url = href
                if (href.startswith('/') and not href.startswith('/home')
                        and '/status/' not in href and not author):
                    author = href.strip('/')

            if '/status/' not in tweet_url:
                continue
            tweet_id = tweet_url.split('/status/')[-1].split('?')[0]
            if not tweet_id or author.lower() in OWN_X_HANDLES:
                continue

            mentions.append({
                'tweet_id': tweet_id,
                'author': author,
                'text': text,
                'url': f"https://x.com{tweet_url}" if tweet_url.startswith('/') else tweet_url,
            })
        except Exception as e:
            print(f"[X] skipped an item: {e}", file=sys.stderr)
            continue

    return mentions


def handle_x_replies(page, dry_run=False):
    handled = []
    try:
        print("Checking X notifications & mentions...", file=sys.stderr)
        mentions = collect_x_mentions(page)

        for m in mentions:
            if len(handled) >= MAX_X_ACTIONS:
                break

            interaction_id = f"x_{m['tweet_id']}"
            # Shared ledger too: the other inbound job reaches the same mention
            # by a different route and mints a different id for it.
            if is_already_handled(interaction_id) or dedupe.already_answered(
                    'x', m['author'], m['text']):
                continue

            reply_text = generate_reply(m['author'], m['text'], 'x')
            if not reply_text:
                continue

            if dry_run:
                print(f"[X][dry-run] would reply to @{m['author']}: {reply_text}", file=sys.stderr)
                handled.append({'platform': 'X', 'author': m['author'],
                                'incoming': m['text'][:100], 'reply': reply_text,
                                'status': 'dry-run'})
                continue

            try:
                page.goto(m['url'], timeout=40000, wait_until="domcontentloaded")
                time.sleep(3)

                reply_btn = first_visible(page, '[data-testid="reply"]')
                if reply_btn is None:
                    print(f"[X] no reply button on {m['url']}", file=sys.stderr)
                    continue

                reply_btn.click()
                time.sleep(2)
                page.keyboard.type(reply_text, delay=10)
                time.sleep(1)
                page.keyboard.press("Control+Enter")
                time.sleep(5)

                record_handled('x', interaction_id, m['author'], m['text'][:200], reply_text)
                dedupe.mark_answered('x', m['author'], m['text'], reply_text,
                                     source='auto_respond_social_replies')
                handled.append({'platform': 'X', 'author': m['author'],
                                'incoming': m['text'][:100], 'reply': reply_text})
                print(f"[X] replied to @{m['author']}", file=sys.stderr)
            except Exception as e:
                print(f"[X] failed replying to @{m['author']}: {e}", file=sys.stderr)
                continue

    except Exception as e:
        print(f"[X] error: {e}", file=sys.stderr)
    return handled


# --------------------------------------------------------------------------
# LinkedIn
# --------------------------------------------------------------------------

def post_url_from_href(href):
    """Normalise a notification card's link into a post permalink, or None.

    LinkedIn percent-encodes the urn and usually points at the feed with a
    ?highlightedUpdateUrn= param rather than a /feed/update/ path, so a plain
    substring test for 'urn:li:activity' never matches the raw href.
    """
    if not href:
        return None
    decoded = unquote(href)

    if 'highlightedUpdateUrn=' in decoded:
        try:
            urn = (parse_qs(urlparse(decoded).query).get('highlightedUpdateUrn') or [None])[0]
            if urn:
                return f"https://www.linkedin.com/feed/update/{urn}/"
        except Exception:
            pass

    if '/feed/update/' in decoded or '/posts/' in decoded:
        return 'https://www.linkedin.com' + decoded if decoded.startswith('/') else decoded

    return None


def collect_linkedin_post_urls(page):
    """Harvest post URLs from notification cards without clicking them."""
    page.goto("https://www.linkedin.com/notifications/", timeout=40000, wait_until="domcontentloaded")
    time.sleep(5)

    cards = page.locator('article.nt-card, div.nt-card, div[data-view-name="notification-card"]').all()
    print(f"[LI] {len(cards)} notification cards on page.", file=sys.stderr)

    urls = []
    seen = set()
    for card in cards:
        try:
            txt = card.inner_text().lower()
            if not any(k in txt for k in ['commented on your', 'replied to your', 'mentioned you']):
                continue
            for link in card.locator('a').all():
                url = post_url_from_href(link.get_attribute('href'))
                if url:
                    if url not in seen:
                        seen.add(url)
                        urls.append(url)
                    break
        except Exception as e:
            print(f"[LI] skipped a card: {e}", file=sys.stderr)
            continue

    return urls[:MAX_LI_POSTS]


def handle_linkedin_replies(page, dry_run=False):
    handled = []
    try:
        print("Checking LinkedIn post comments & notifications...", file=sys.stderr)
        post_urls = collect_linkedin_post_urls(page)
        print(f"[LI] {len(post_urls)} posts with comment/mention notifications.", file=sys.stderr)

        for url in post_urls:
            try:
                # Deep comment permalinks carry a long commentUrn query string
                # and load slowly; 45s tripped on them. 'commit' returns once
                # navigation starts rather than waiting for the full document,
                # which is enough since we poll for elements afterwards.
                page.goto(url, timeout=70000, wait_until="commit")
                time.sleep(4)

                post_urn = url.rstrip('/').split('/')[-1].split('?')[0]
                comments = page.locator('article.comments-comment-entity, .comments-comment-item').all()
                print(f"[LI] {len(comments)} comments on {post_urn}", file=sys.stderr)

                posted_here = 0
                for cb in comments:
                    if posted_here >= MAX_LI_REPLIES_PER_POST:
                        break
                    try:
                        # The comment BODY only. cb.inner_text() also pulls in the
                        # author's name, "Author" badge and job headline, which the
                        # response rules then matched on -- producing replies keyed
                        # off someone's job title rather than what they said.
                        comment_text = text_of(
                            cb,
                            '.comments-comment-item__main-content, '
                            '.comments-comment-item-content-body, '
                            '.attributed-text-segment-list__content',
                        )
                        if not comment_text:
                            continue

                        author = text_of(
                            cb,
                            '.comments-comment-meta__description-title, '
                            '.comments-post-meta__name-text, '
                            '.comments-comment-entity__author-name',
                            default='Connection',
                        )
                        if OWN_LI_NAME in author:
                            continue

                        # Only answer comments actually addressed to Vishal. LinkedIn
                        # prefixes the addressee's name in the body, so a thread's
                        # other comments ("Darwin Fisk Absolutely agree...") are
                        # between third parties -- replying to those spams strangers.
                        if OWN_LI_NAME not in comment_text:
                            continue

                        interaction_id = stable_id('li', post_urn, author, comment_text[:200])
                        if is_already_handled(interaction_id) or dedupe.already_answered(
                                'linkedin', author, comment_text):
                            continue

                        reply_text = generate_reply(author, comment_text, 'linkedin')
                        if not reply_text:
                            continue

                        if dry_run:
                            print(f"[LI][dry-run] would reply to {author} on {post_urn}: {reply_text}",
                                  file=sys.stderr)
                            handled.append({'platform': 'LinkedIn', 'author': author,
                                            'incoming': comment_text[:100], 'reply': reply_text,
                                            'status': 'dry-run'})
                            posted_here += 1
                            continue

                        # Reply button lives inside the comment; the editor and
                        # submit button LinkedIn injects afterwards do not --
                        # scoping those to the comment was why nothing ever posted.
                        reply_btn = first_visible(
                            cb,
                            'button.comments-comment-social-bar__reply-action-button, '
                            'button:has-text("Reply")',
                        )
                        if reply_btn is None:
                            continue
                        reply_btn.click()
                        time.sleep(2)

                        editor = reply_editor_for(page, cb)
                        if editor is None:
                            print(f"[LI] reply editor never appeared on {post_urn}", file=sys.stderr)
                            continue

                        editor.click()
                        time.sleep(0.3)
                        page.keyboard.type(reply_text, delay=8)
                        time.sleep(1)

                        submit = first_visible(page, 'button.comments-comment-box__submit-button')
                        if submit is None:
                            submit = first_visible(
                                page, 'button:has-text("Reply"), button:has-text("Post")')
                        if submit is None:
                            print(f"[LI] submit button not found on {post_urn}", file=sys.stderr)
                            continue

                        submit.click()
                        time.sleep(4)

                        # Verify it actually landed. Recording unconditionally
                        # meant a silently failed submit was stored as 'posted',
                        # and the dedupe key then blocked any retry forever.
                        probe = reply_text[:60]
                        try:
                            landed = probe in page.inner_text('body')
                        except Exception:
                            landed = False
                        if not landed:
                            print(f"[LI] submit did not appear on {post_urn}; "
                                  f"not recording so it can retry", file=sys.stderr)
                            continue

                        record_handled('linkedin', interaction_id, author,
                                       comment_text[:200], reply_text)
                        dedupe.mark_answered('linkedin', author, comment_text, reply_text,
                                             source='auto_respond_social_replies')
                        handled.append({'platform': 'LinkedIn', 'author': author,
                                        'incoming': comment_text[:100], 'reply': reply_text})
                        posted_here += 1
                        print(f"[LI] replied to {author} on {post_urn}", file=sys.stderr)
                    except Exception as e:
                        print(f"[LI] comment skipped: {e}", file=sys.stderr)
                        continue
            except Exception as e:
                print(f"[LI] post {url} failed: {e}", file=sys.stderr)
                continue

    except Exception as e:
        print(f"[LI] error: {e}", file=sys.stderr)
    return handled


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------

def run_reply_monitor(dry_run=False):
    init_tables()
    total_handled = []

    for lock_name in ['SingletonLock', 'SingletonSocket', 'SingletonCookie']:
        p = os.path.join(AUTOMATION_PROFILE_DIR, lock_name)
        if os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass

    with sync_playwright() as p:
        context = None
        try:
            context = p.chromium.launch_persistent_context(
                user_data_dir=AUTOMATION_PROFILE_DIR,
                channel="chrome",
                headless=True,
                args=['--disable-blink-features=AutomationControlled', '--no-sandbox'],
            )
            page = context.new_page()
            page.set_viewport_size({"width": 1440, "height": 900})

            total_handled.extend(handle_x_replies(page, dry_run=dry_run))
            total_handled.extend(handle_linkedin_replies(page, dry_run=dry_run))
        except Exception as e:
            print(f"Error in run_reply_monitor: {e}", file=sys.stderr)
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass

    return total_handled


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true',
                        help='scan and report what would be replied to; post nothing')
    args = parser.parse_args()

    # Serialise against every other job on the shared Chrome profile.
    # Without this, two jobs launch Chrome on one profile and one dies with
    # "Failed to create a ProcessSingleton for your profile directory".
    try:
        with profile_lock('social_all_reply_inbound'):
            results = run_reply_monitor(dry_run=args.dry_run)
    except BrowserBusy as e:
        print(f"Skipping run: {e}", file=sys.stderr)
        results = []
    print(json.dumps(results, indent=2))
