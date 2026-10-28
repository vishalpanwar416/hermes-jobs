"""LinkedIn growth commenter — two-phase (find / post).

Phase 1:  find [--max N] [--min-reactions N]
    Scans the feed and prints JSON candidates: genuine tech discussions or
    high-reach posts with dev context. Each candidate carries the post's real
    LinkedIn URN so it can be revisited by permalink.

Phase 2:  post <urn> <comment text...>
    Opens the post's permalink and posts the given comment (written by the
    agent to relate to the post's actual content), then records it in the DB.

Legacy:   a bare numeric first arg (old cron prompt style) maps to find mode.

DOM notes (2026-08): feed and search now serve LinkedIn's obfuscated frontend —
hashed class names, no data-urn/feed-shared-update-v2. Posts are located
generically: the parent element with the most direct children whose innerText
starts with "Feed post". Activity URNs only sometimes appear inline (permalink
anchors / comment componentkeys); when missing they are resolved via the post's
control menu -> "Copy link to post" -> clipboard (share URLs embed the activity
id). window.scrollBy doesn't reach the inner scroll container — lazy load is
triggered by scrollIntoView on the last post. Permalink pages still render the
classic UI (artdeco, ql-editor), so the post phase keeps classic selectors.
"""
import os
import re
import sys
import time
import json
import sqlite3
from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# Capture this run's full stdout and stderr to
# ~/.hermes/logs/pipelines/linkedin_growth_comments/ so a failed or silent run can be
# diagnosed afterwards instead of vanishing.
if __name__ == '__main__':
    # Only when run directly. Firing on import made every script that
    # imports this module log its own run under this pipeline's name.
    try:
        import pipeline_log as _plog
        _plog.start('linkedin_growth_comments')
    except Exception:
        pass

import social_voice as voice

DB_PATH = os.path.expanduser('~/.hermes/data/x_growth.db')
AUTOMATION_PROFILE_DIR = os.path.expanduser('~/.hermes/data/chrome_automation_profile')

# Strict negative keywords: NEVER touch job ads, recruiters, hiring alerts,
# personal-life updates, or certificate spam.
BANNED_POST_KEYWORDS = [
    'hiring', 'we are hiring', "we're hiring", 'job opening', 'job opportunity',
    'apply now', 'send resume', 'share your cv', 'looking for a', 'recruiting',
    'talent acquisition', 'internship', 'fresher', 'urgent requirement', 'open role',
    'referral alert', 'referral for', 'dm me for referral',
    'congratulations on', 'happy birthday', 'joined', 'starting a new position',
    'certificate', 'completed course', 'certified',
]

# A post qualifies as a tech discussion when it hits these signals.
TECH_CONTEXT_KEYWORDS = [
    'golang', 'go lang', 'goroutine', 'sync.pool', 'concurrency', 'gin framework',
    'python', 'typescript', 'rust', 'java ', 'backend', 'frontend', 'full stack',
    'api design', 'rest api', 'grpc', 'websocket', 'microservice', 'monolith',
    'mcp', 'model context protocol', 'ai agent', 'agentic', 'llm', 'rag',
    'prompt engineering', 'fine-tuning', 'fine-tune', 'embedding', 'vector db', 'evals',
    'redis', 'postgres', 'postgresql', 'mysql', 'mongodb', 'database', 'indexing',
    'caching', 'thundering herd', 'sharding', 'replication',
    'kubernetes', 'k8s', 'docker', 'terraform', 'ci/cd', 'devops', 'sre',
    'observability', 'disaster recovery', 'failover', 'latency', 'p95', 'p99',
    'throughput', 'system design', 'distributed system', 'architecture',
    'scalability', 'load balancer', 'load balancing', 'message queue', 'kafka', 'event driven',
    'open source', 'github', 'code review', 'refactor', 'tech debt',
    'software engineer', 'debugging', 'production incident', 'postmortem',
]

# Feed-post header lines that precede the actual commentary.
CONTEXT_LINE_RE = re.compile(
    r'^(feed post|suggested|promoted|follow|.* commented( on this)?|'
    r'.* (likes|celebrates|loves|supports) this|.* reposted this)$', re.I)
TIME_LINE_RE = re.compile(r'^\d+\s*(m|h|d|w|mo|yr?)\b.*•|^just now', re.I)
FOOTER_LINE_RE = re.compile(
    r'^(like|comment|repost|send|share|save)$|^\d[\d,.]*\s*[KkMm]?$|'
    r'^[\d,.]+[KkMm]?\s+comments?$|^[\d,.]+[KkMm]?\s+reposts?$|'
    r'^activate to view|^…more$|^see more$', re.I)

# Locate the post-list container: the parent with the most direct "Feed post"
# children. Works on both the feed and content-search pages.
_FIND_LIST_JS = """
  const parents = new Map();
  for (const e of document.querySelectorAll('div,li')) {
    const t = e.innerText || '';
    if (!t.startsWith('Feed post')) continue;
    const p = e.parentElement;
    if (p) parents.set(p, (parents.get(p) || 0) + 1);
  }
  let best = null, bestN = 0;
  for (const [p, n] of parents) if (n > bestN) { best = p; bestN = n; }
"""

FEED_SCRAPE_JS = "() => {" + _FIND_LIST_JS + """
  if (!best) return null;
  const out = [];
  Array.from(best.children).forEach((k, idx) => {
    const text = (k.innerText || '').trim();
    if (!text.startsWith('Feed post')) return;
    k.setAttribute('data-lgc-idx', String(idx));
    // NOTE: inline URNs in the container HTML are unreliable — comment keys and
    // embedded anchors often reference a DIFFERENT activity. The control menu's
    // "Copy link to post" is the only authoritative source (see _resolve_urn_via_menu).
    const urn = null;
    const nums = [];
    k.querySelectorAll('a,button,span').forEach(e => {
      const t = (e.innerText || '').trim();
      if (/^[\\d,.]+[KkMm]?$/.test(t) && t.length < 12) nums.push(t);
    });
    out.push({idx, urn, text: text.slice(0, 4000), nums});
  });
  return out;
}"""

FEED_SCROLL_JS = "() => {" + _FIND_LIST_JS + """
  if (!best || !best.children.length) return false;
  best.children[best.children.length - 1].scrollIntoView({block: 'end'});
  return true;
}"""

# Content-search queries used to supplement the feed (which is mostly job spam).
SEARCH_QUERIES = [
    'system design', 'distributed systems', 'golang concurrency', 'backend engineering',
    'kubernetes production', 'postgres performance', 'ai agents', 'llm engineering',
    'microservices architecture', 'api design',
]


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute('''
    CREATE TABLE IF NOT EXISTS linkedin_comments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        post_urn TEXT UNIQUE NOT NULL,
        author TEXT,
        post_text TEXT,
        comment_text TEXT,
        status TEXT DEFAULT 'posted',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    conn.commit()
    conn.close()


def is_post_commented(post_ref):
    key = _post_key(post_ref)
    conn = sqlite3.connect(DB_PATH)
    # The table uses post_hash as PK; check both column naming conventions
    try:
        row = conn.execute(
            "SELECT rowid FROM linkedin_comments WHERE post_urn LIKE ?", (f'%{key}%',)
        ).fetchone()
    except Exception:
        row = conn.execute(
            "SELECT rowid FROM linkedin_comments WHERE post_hash LIKE ?", (f'%{key}%',)
        ).fetchone()
    conn.close()
    return row is not None


def record_comment(post_urn, author, post_text, comment_text):
    conn = sqlite3.connect(DB_PATH)
    # Check which column name the existing table uses
    try:
        conn.execute("SELECT post_urn FROM linkedin_comments LIMIT 1")
        # created_at is written explicitly: the live table predates the schema
        # above and has no default, so rows landed with created_at NULL and
        # neither the daily cap nor comment_text_reused() could see them
        # (found 2026-09-12 after the first script-mode post).
        conn.execute('''
        INSERT OR IGNORE INTO linkedin_comments (post_urn, author, post_text, comment_text, status, created_at)
        VALUES (?, ?, ?, ?, 'posted', datetime('now'))
        ''', (post_urn, author, post_text, comment_text))
    except Exception:
        conn.execute('''
        INSERT OR IGNORE INTO linkedin_comments (post_hash, author, post_text, comment_text, status, created_at)
        VALUES (?, ?, ?, ?, 'posted', datetime('now'))
        ''', (post_urn, author, post_text, comment_text))
    conn.commit()
    conn.close()


def _norm(text):
    return re.sub(r'[^a-z0-9 ]+', '', (text or '').lower()).strip()


def comment_text_reused(comment_text, days=60):
    """Has this comment, or its first sentence, gone out before?

    On 2026-09-03 the same "The best system designs use boring, reliable
    components..." was posted under four different people's posts inside six
    minutes, and "Goroutines are cheap, but channels are where..." under two
    more. The agent that writes comments is told every one must be unique; it
    does not remember across runs, so the memory has to live here.
    """
    key = _norm(comment_text)
    head = ' '.join(key.split()[:12])
    if not key:
        return False
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            "SELECT comment_text FROM linkedin_comments "
            "WHERE created_at >= datetime('now', ?)", (f'-{int(days)} days',)).fetchall()
    except sqlite3.Error:
        return False
    finally:
        conn.close()
    for (old,) in rows:
        o = _norm(old)
        if not o:
            continue
        if o == key or (head and o.startswith(head)):
            return True
    return False


def _clear_profile_locks():
    for lock in ['SingletonLock', 'SingletonSocket', 'SingletonCookie']:
        p = os.path.join(AUTOMATION_PROFILE_DIR, lock)
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass


def _launch(p):
    _clear_profile_locks()
    context = p.chromium.launch_persistent_context(
        user_data_dir=AUTOMATION_PROFILE_DIR,
        channel="chrome",
        headless=True,
        args=[
            '--disable-blink-features=AutomationControlled',
            '--no-sandbox',
            '--disable-dev-shm-usage',
            '--disable-gpu',
        ],
    )
    context.grant_permissions(['clipboard-read', 'clipboard-write'],
                              origin='https://www.linkedin.com')
    page = context.pages[0] if context.pages else context.new_page()
    page.set_viewport_size({"width": 1440, "height": 900})
    return context, page


def _parse_count(raw):
    """'1,234' / '1.2K' / '3 comments' -> int."""
    if not raw:
        return 0
    m = re.search(r'([\d,.]+)\s*([KkMm]?)', raw)
    if not m:
        return 0
    num = m.group(1).replace(',', '').rstrip('.')
    try:
        val = float(num)
    except ValueError:
        return 0
    suffix = m.group(2).lower()
    if suffix == 'k':
        val *= 1_000
    elif suffix == 'm':
        val *= 1_000_000
    return int(val)


def _parse_feed_item(item):
    """Split a raw feed item's innerText into author + commentary body."""
    lines = [l.strip() for l in item['text'].split('\n')]
    author = ''
    body_start = 0
    for i, line in enumerate(lines[:20]):
        if not line or CONTEXT_LINE_RE.match(line):
            continue
        if not author and not TIME_LINE_RE.match(line) and '•' not in line:
            author = line
            continue
        if TIME_LINE_RE.match(line):
            body_start = i + 1
            break
    body_lines = []
    for line in lines[body_start:]:
        if not line:
            continue
        if CONTEXT_LINE_RE.match(line) or TIME_LINE_RE.match(line):
            continue
        if FOOTER_LINE_RE.match(line):
            # social bar / action buttons mark the end of the commentary
            if body_lines:
                break
            continue
        body_lines.append(line)
    body = '\n'.join(body_lines).strip()
    reactions = max((_parse_count(n) for n in item.get('nums', [])), default=0)
    return {
        'urn': item.get('urn'),
        'author': author or 'Unknown',
        'text': body,
        'reactions': reactions,
        'comments': 0,
    }


# Structured job-ad markers: two or more of these means a listing, not a discussion.
JOB_AD_MARKERS = [
    'company:', 'location:', 'experience:', 'salary:', 'ctc:', 'notice period',
    'role overview', 'key responsibilities', 'requirements', 'qualifications',
    'perks & benefits', 'benefits', 'work mode', 'job description', 'interested candidates',
]


def _looks_like_job_ad(text_lower):
    return sum(1 for m in JOB_AD_MARKERS if m in text_lower) >= 2


def _matched_tech_keywords(text):
    t = text.lower()
    # word-boundary match so 'rag' doesn't hit "encouragement" or 'rust' "trust"
    return [k for k in TECH_CONTEXT_KEYWORDS
            if re.search(r'(?<![a-z0-9])' + re.escape(k.strip()) + r'(?![a-z0-9])', t)]


def _post_key(ref):
    """Canonical dedupe key for a post: the numeric id in its link/urn."""
    m = re.search(r'(\d{15,25})', ref or '')
    return m.group(1) if m else (ref or '')


def _expand_short_link(page, short_url):
    """Resolve a lnkd.in shortlink to the canonical post URL.

    LinkedIn's "Copy link to post" now yields https://lnkd.in/p/<code>, which
    carries no numeric activity id. That broke candidate discovery outright:
    the short form matches neither '/posts/' nor '/feed/update/', so every
    candidate was discarded and the commenter went silent. _post_key() also
    needs the numeric id, otherwise dedupe stops matching the existing rows.
    """
    try:
        import urllib.request
        req = urllib.request.Request(
            short_url,
            headers={
                'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            }
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            final = (resp.geturl() or '').split('?')[0]
            if '/posts/' in final or '/feed/update/' in final:
                return final
            print(f"Short link {short_url} resolved to unexpected {final}", file=sys.stderr)
    except Exception as e:
        print(f"Short link expansion failed for {short_url}: {e}", file=sys.stderr)
    return None


def _resolve_share_link(page, idx):
    """Get a post's share URL from its control menu -> Copy link to post.

    The trailing number in the copied /posts/... URL is a share id, NOT an
    activity id — /feed/update/urn:li:activity:<id>/ built from it 404s, so we
    keep and navigate the share URL itself.
    """
    try:
        btn = page.locator(
            f'[data-lgc-idx="{idx}"] button[aria-label^="Open control menu"]').first
        if not btn.count():
            return None
        btn.scroll_into_view_if_needed()
        btn.click()
        time.sleep(1.5)
        item = page.locator('[role="menuitem"]:has-text("Copy link to post"), '
                            'li:has-text("Copy link to post")').first
        if not item.count():
            page.keyboard.press('Escape')
            return None
        item.click()
        time.sleep(1.5)
        link = (page.evaluate("navigator.clipboard.readText()") or '').split('?')[0]
        if 'lnkd.in' in link:
            link = _expand_short_link(page, link) or link
        if '/posts/' in link or '/feed/update/' in link:
            return link
        print(f"Unusable share link for idx {idx}: {link!r}", file=sys.stderr)
    except Exception as e:
        print(f"Share-link resolution failed for idx {idx}: {e}", file=sys.stderr)
        try:
            page.keyboard.press('Escape')
        except Exception:
            pass
    return None


def _scan_page(page, candidates, max_candidates, min_reactions, scrolls, source):
    """Scroll one loaded page (feed or search results), collecting candidates."""
    seen_idx = set()
    for scroll_i in range(scrolls):
        if len(candidates) >= max_candidates:
            return
        items = page.evaluate(FEED_SCRAPE_JS)
        if items is None:
            print(f"[{source}] post list not found — layout changed again?",
                  file=sys.stderr)
            return
        print(f"[{source}] scroll {scroll_i}: {len(items)} items", file=sys.stderr)
        for item in items:
            if len(candidates) >= max_candidates:
                return
            if item['idx'] in seen_idx:
                continue
            seen_idx.add(item['idx'])
            post = _parse_feed_item(item)
            t = post['text'].lower()
            if len(post['text']) < 80 or 'vishal panwar' in t:
                continue
            if any(b in t for b in BANNED_POST_KEYWORDS) or _looks_like_job_ad(t):
                continue

            matched = _matched_tech_keywords(post['text'])
            high_reach = post['reactions'] >= min_reactions
            # Tech discussion (>=2 tech signals), or high-reach post
            # with at least some dev context (>=1 signal).
            if not (len(matched) >= 2 or (high_reach and matched)):
                continue

            link = _resolve_share_link(page, item['idx'])
            if not link:
                continue
            key = _post_key(link)
            if key in candidates or is_post_commented(key):
                continue
            post['url'] = link
            post['urn'] = key
            post['matched_keywords'] = matched[:6]
            post['high_reach'] = high_reach
            post['text'] = post['text'][:1200]
            post['source'] = source
            candidates[post['urn']] = post
            print(f"Candidate [{source}]: {post['author']} | reactions={post['reactions']} "
                  f"| keywords={matched[:4]}", file=sys.stderr)
        page.evaluate(FEED_SCROLL_JS)
        time.sleep(4)


def find_candidates(max_candidates=5, min_reactions=100, scrolls=6):
    """Scan the feed + content search for tech discussions / high-reach dev posts."""
    import random
    init_db()
    candidates = {}

    _clear_profile_locks()
    with sync_playwright() as p:
        context, page = _launch(p)
        try:
            print("Navigating to LinkedIn feed...", file=sys.stderr)
            page.goto("https://www.linkedin.com/feed/", timeout=45000,
                      wait_until="domcontentloaded")
            time.sleep(8)
            print(f"Current URL: {page.url} | Title: {page.title()}", file=sys.stderr)
            _scan_page(page, candidates, max_candidates, min_reactions, scrolls, 'feed')

            # The feed is mostly job spam; content search finds real discussions.
            for query in random.sample(SEARCH_QUERIES, k=2):
                if len(candidates) >= max_candidates:
                    break
                url = ('https://www.linkedin.com/search/results/content/?keywords='
                       + query.replace(' ', '%20'))
                page.goto(url, timeout=45000, wait_until="domcontentloaded")
                time.sleep(8)
                _scan_page(page, candidates, max_candidates, min_reactions,
                           max(scrolls - 2, 3), f'search:{query}')
        finally:
            context.close()

    # Highest-reach first so comments land where visibility is best.
    return sorted(candidates.values(), key=lambda x: x['reactions'], reverse=True)


_EDITOR_SEL = ('div.ql-editor[contenteditable="true"], '
               'div[role="textbox"][contenteditable="true"]')

# Around 2026-09-21 permalink pages moved to the obfuscated frontend too: the
# classic .update-components-* selectors match nothing, so every candidate was
# skipped as "post text not found". The post body is the first
# expandable-text-box (comment bodies use the same testid further down), the
# post-level composer is labelled "Text editor for creating comment", and the
# submit is a bare <button>Comment</button> (the action-bar Comment button
# carries aria-label="Comment", which is how the two are told apart).
_NEW_EDITOR_SEL = 'div[contenteditable="true"][aria-label="Text editor for creating comment"]'
_MENU_PREFIX = 'Open control menu for post by '


def _read_post(page):
    """(author, post_text) from a permalink page, new UI first, classic second."""
    author, post_text = 'Unknown', ''
    loc = page.locator('[data-testid="expandable-text-box"], '
                       '.update-components-text, .feed-shared-inline-show-more-text').first
    if loc.count():
        post_text = (loc.inner_text() or '').strip()
    loc = page.locator(f'button[aria-label^="{_MENU_PREFIX}"]').first
    if loc.count():
        author = (loc.get_attribute('aria-label') or '')[len(_MENU_PREFIX):].strip() or author
    else:
        loc = page.locator('.update-components-actor__title span[aria-hidden="true"], '
                           '.update-components-actor__title').first
        if loc.count():
            author = (loc.inner_text() or '').strip().split('\n')[0]
    return author, post_text


def _submit_button(page):
    submit = page.locator('button.comments-comment-box__submit-button').first
    if submit.count() and submit.is_visible():
        return submit
    for b in page.locator('button:not([aria-label])').filter(
            has_text=re.compile(r'^\s*Comment\s*$')).all():
        if b.is_visible():
            return b
    return None

_IN_COMMENT_JS = ("n => !!n.closest('article.comments-comment-entity, "
                  ".comments-comment-item, .comments-comment-entity')")


def _post_level_editor(page):
    """The POST's own comment box, not a reply box nested under a comment.

    page.locator(_EDITOR_SEL).first returns the first editor in DOM order, and
    LinkedIn renders inline reply boxes inside the comment list, above the post
    composer. That is how comments meant as top level ended up posted as replies
    under other people's comments ("Vishal replied to X's comment").
    """
    e = page.locator(_NEW_EDITOR_SEL).first
    if e.count() and e.is_visible():
        return e
    for e in page.locator(_EDITOR_SEL).all():
        try:
            if not e.is_visible():
                continue
            if e.evaluate(_IN_COMMENT_JS):
                continue  # this one belongs to a comment, not the post
            return e
        except Exception:
            continue
    return None


def fetch_post(ref):
    """Open a post by share URL or urn and return (author, full_text, url).

    Feed cards hand find_candidates a truncated body with LinkedIn chrome
    around it; social_voice.draft_comment given that text said SKIP on 16 of 16
    engineering posts on 2026-09-12, and given the same post's full permalink
    text it drafted a relevant comment 3 of 3 times. So the drafter reads the
    permalink, which still serves the classic UI and its stable selectors.
    """
    url = ref if ref.startswith('http') else f"https://www.linkedin.com/feed/update/{ref}/"
    _clear_profile_locks()
    with sync_playwright() as p:
        context, page = _launch(p)
        try:
            page.goto(url, timeout=45000, wait_until="domcontentloaded")
            time.sleep(5)
            author, post_text = _read_post(page)
            return author, post_text, page.url
        finally:
            context.close()


def post_comment(ref, comment_text, dry_run=False):
    """Open the post (share URL or urn) and publish the agent-written comment.

    dry_run does everything except the final submit click: it reports whether
    the post text, comment button and editor were found and what the judge
    scored, so the pipeline can be exercised without posting anything.
    """
    init_db()
    if is_post_commented(ref):
        return {'status': 'skipped', 'reason': 'already commented on this post', 'urn': ref}

    # This comment was written by the cron agent, not by social_voice, so this is
    # the only gate where dashes, bullets, hashtags and markdown can be caught
    # before they reach LinkedIn.
    original = comment_text
    comment_text = voice.humanize(comment_text, limit=400)
    if comment_text != original:
        print(f"[voice] scrubbed comment before posting: {voice.has_ai_tells(original)}",
              file=sys.stderr)

    if len(comment_text) < 30:
        return {'status': 'error', 'urn': ref,
                'reason': 'comment too short, write a substantive, post-specific comment'}
    if comment_text_reused(comment_text):
        return {'status': 'error', 'urn': ref,
                'reason': 'this comment (or its opening) was already posted under another '
                          'post in the last 60 days. Write one that responds to THIS post.'}

    _clear_profile_locks()
    with sync_playwright() as p:
        context, page = _launch(p)
        try:
            if ref.startswith('http'):
                url = ref
            else:
                url = f"https://www.linkedin.com/feed/update/{ref}/"
            print(f"Opening {url}", file=sys.stderr)
            page.goto(url, timeout=45000, wait_until="domcontentloaded")
            time.sleep(5)

            author, post_text = _read_post(page)

            # The agent wrote this comment from a feed snippet it may or may not
            # have read. This is the first point where the post's actual text
            # and the comment are both in hand, so it is where relevance gets
            # checked. Unscorable means unposted: the failure mode being
            # prevented is a stranger reading an off-topic comment under their
            # post, and a skipped comment costs nothing.
            score = None
            if post_text:
                score = voice.judge_relevance(post_text, comment_text)
                if score is None or score < voice.RELEVANCE_MIN:
                    return {'status': 'rejected', 'urn': ref, 'relevance': score,
                            'author': author, 'post_snippet': post_text[:160],
                            'reason': f'relevance {score}/5 is below {voice.RELEVANCE_MIN}. '
                                      'The comment does not respond to what this post says. '
                                      'Read the post_snippet and either write a comment about '
                                      'its specific point or skip this post.'}
                print(f"[judge] {score}/5 for comment on {author}'s post", file=sys.stderr)
            else:
                print('[judge] post text not found on page, skipping relevance check',
                      file=sys.stderr)

            btn = page.locator('button[aria-label="Comment"]').first
            if not btn.count() or not btn.is_visible():
                return {'status': 'error', 'urn': ref,
                        'reason': 'comment button not found (post may be deleted or restricted)'}
            btn.scroll_into_view_if_needed()
            time.sleep(1)
            btn.click()
            time.sleep(2)

            editor = _post_level_editor(page)
            if editor is None:
                return {'status': 'error', 'urn': ref, 'reason': 'comment editor did not open'}
            editor.click()
            time.sleep(0.3)
            page.keyboard.type(comment_text, delay=6)
            time.sleep(1.5)

            submit = _submit_button(page)
            if submit is None:
                return {'status': 'error', 'urn': ref, 'reason': 'submit button not found'}
            if dry_run:
                return {
                    'status': 'dry_run',
                    'urn': _post_key(ref),
                    'url': url,
                    'author': author,
                    'post_snippet': post_text[:160],
                    'comment': comment_text,
                    'relevance': score if post_text else None,
                    'note': 'comment box and submit button found; nothing was posted',
                }
            submit.click()
            time.sleep(5)
            # The editor empties once LinkedIn accepts the comment; text still
            # sitting in it means the click did not submit.
            if len((editor.inner_text() or '').strip()) > 10:
                return {'status': 'error', 'urn': ref,
                        'reason': 'submit clicked but the comment is still in the editor'}

            record_comment(_post_key(ref), author, post_text[:200], comment_text)
            return {
                'status': 'posted',
                'urn': _post_key(ref),
                'url': url,
                'author': author,
                'post_snippet': post_text[:100],
                'comment': comment_text,
            }
        finally:
            context.close()


def main():
    args = sys.argv[1:]
    if args and args[0] == 'post':
        if len(args) < 3:
            print(json.dumps({'status': 'error', 'reason': 'usage: post <urn> <comment text...>'}))
            sys.exit(1)
        dry = False
        if args[1] == '--dry-run':
            dry = True
            args = [args[0]] + args[2:]
        urn = args[1]
        comment = ' '.join(args[2:]).strip()
        print(json.dumps(post_comment(urn, comment, dry_run=dry), indent=2))
        return

    # find mode (default; a bare number = legacy invocation)
    max_candidates, min_reactions = 5, 100
    if args and args[0] == 'find':
        args = args[1:]
    it = iter(args)
    for a in it:
        if a == '--max':
            max_candidates = int(next(it, max_candidates))
        elif a == '--min-reactions':
            min_reactions = int(next(it, min_reactions))
        elif a.isdigit():
            max_candidates = max(int(a), 3)
    print(json.dumps(find_candidates(max_candidates, min_reactions), indent=2))


if __name__ == '__main__':
    main()
