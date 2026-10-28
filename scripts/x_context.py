#!/usr/bin/env python3
"""Read a tweet properly before replying to it.

Every X commenting path in this repo has been drafting replies from one string:
`article.inner_text()` as it appears on a search or feed card. That string is the
worst available version of the post. X truncates it at roughly 280 rendered
characters behind a "Show more", it drops the tweet the post is quoting, it drops
the tweet the post is replying to, it carries image alt text nowhere, and it says
nothing about who wrote it or what has already been said underneath.

That is why the reply to @wagslane opened with unbounded goroutines under a joke
about online learning (X_GROWTH_PLAN.md section 5), and it is most of why
draft_comment() returns SKIP so often: handed four lines of a twelve line post,
the honest answer really is "there is nothing here to engage with".

fetch_context() opens the permalink instead. On a status page the focal tweet is
never truncated, the thread parent and the quoted tweet are in the DOM, the
metrics are exact, and the existing replies are right there — which is the one
piece of context that decides whether a reply is worth posting at all, because
the fifth person to make the same point gets no reach from it.

Author profiles are fetched once and cached for two weeks in x_growth.db. That
cache is what makes topic search usable: "is this author worth replying to" is a
question about the author, and until now nothing in the pipeline could ask it.
"""
import os
import re
import sys
import json
import sqlite3
import time
from datetime import datetime, timezone

DB_PATH = os.path.expanduser('~/.hermes/data/x_growth.db')

# Two weeks. Follower counts move slowly enough that a stale number never
# changes a reply/skip decision, and re-reading every profile on every run would
# double the page loads this job makes.
AUTHOR_TTL_DAYS = 14


# ---------------------------------------------------------------------------
# author cache
# ---------------------------------------------------------------------------

def init_tables():
    conn = sqlite3.connect(DB_PATH)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS x_authors (
            handle TEXT PRIMARY KEY,
            display_name TEXT,
            bio TEXT,
            followers INTEGER,
            following INTEGER,
            fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
    conn.commit()
    conn.close()


def _cached_author(handle):
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            "SELECT display_name, bio, followers, following FROM x_authors "
            "WHERE lower(handle)=lower(?) AND fetched_at >= datetime('now', ?)",
            (handle, f'-{AUTHOR_TTL_DAYS} days')).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if not row:
        return None
    return {'handle': handle, 'display_name': row[0], 'bio': row[1] or '',
            'followers': row[2] or 0, 'following': row[3] or 0}


def _store_author(a):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO x_authors (handle, display_name, bio, followers, following, fetched_at) "
        "VALUES (?,?,?,?,?,CURRENT_TIMESTAMP) "
        "ON CONFLICT(handle) DO UPDATE SET display_name=excluded.display_name, "
        "bio=excluded.bio, followers=excluded.followers, following=excluded.following, "
        "fetched_at=CURRENT_TIMESTAMP",
        (a['handle'], a.get('display_name', ''), a.get('bio', ''),
         a.get('followers', 0), a.get('following', 0)))
    conn.commit()
    conn.close()


def _parse_count(raw):
    """"12.3K" -> 12300, "28.6K Followers" -> 28600. X renders counts this way.

    Searches for the first number rather than requiring the string to be one.
    An earlier version anchored the pattern to both ends, so it read the bare
    "28.6K" out of an aria-label but returned 0 for the follower link, whose
    inner_text is "28.6K Followers" — which silently gave every author zero
    followers and would have failed every non-target post at the reach floor.
    """
    if not raw:
        return 0
    s = str(raw).strip().replace(',', '').upper()
    m = re.search(r'(\d+(?:\.\d+)?)\s*([KM]?)', s)
    if not m:
        return 0
    try:
        n = float(m.group(1))
    except ValueError:
        return 0
    return int(n * {'': 1, 'K': 1_000, 'M': 1_000_000}[m.group(2)])


def fetch_author(page, handle, force=False):
    """Profile facts for one handle, from the cache when it is fresh.

    Returns a dict with followers=0 and an empty bio when the profile cannot be
    read, which callers must treat as "unknown", never as "small account" — a
    protected or rate limited profile would otherwise be silently demoted.
    """
    handle = (handle or '').lstrip('@')
    if not handle:
        return None
    if not force:
        hit = _cached_author(handle)
        if hit:
            return hit

    out = {'handle': handle, 'display_name': '', 'bio': '',
           'followers': 0, 'following': 0, 'unknown': True}
    try:
        page.goto(f'https://x.com/{handle}', timeout=30000, wait_until='domcontentloaded')
        page.wait_for_timeout(2500)
        try:
            page.wait_for_selector('[data-testid="UserName"]', timeout=8000)
        except Exception:
            pass

        try:
            name = page.locator('[data-testid="UserName"]').first.inner_text(timeout=3000)
            out['display_name'] = name.split('\n')[0].strip()
        except Exception:
            pass
        try:
            out['bio'] = page.locator(
                '[data-testid="UserDescription"]').first.inner_text(timeout=3000).strip()
        except Exception:
            pass

        # The counts live in links to /followers and /following. Reading the
        # anchor text rather than regexing the whole column keeps "1,234
        # Following" from being scraped as the follower count, which is the
        # mistake that makes every account look enormous.
        for key, patterns in (('followers', ('verified_followers', 'followers')),
                              ('following', ('following',))):
            for pat in patterns:
                try:
                    link = page.locator(f'a[href$="/{pat}"]').first
                    if not link.count():
                        continue
                    txt = link.inner_text(timeout=2000)
                    # Strip the label so the number is found whichever side of
                    # it X renders, and whether or not there is a line break.
                    val = _parse_count(re.sub(r'(?i)followers?|following', ' ', txt))
                    if val:
                        out[key] = val
                        break
                except Exception:
                    continue

        # "Known" means the follower count was actually read. A profile whose
        # bio loaded but whose counts did not is unknown, not a zero follower
        # account — score_post() rejects the latter and stays neutral on the
        # former, so conflating them would quietly drop good candidates.
        if out['followers']:
            out['unknown'] = False
            _store_author(out)
    except Exception as e:
        print(f'[ctx] could not read profile @{handle}: {e}', file=sys.stderr)
    return out


# ---------------------------------------------------------------------------
# the post itself
# ---------------------------------------------------------------------------

_METRIC_RE = re.compile(
    r'([\d.,]+)\s+(repl(?:y|ies)|repost|reposts|like|likes|view|views|bookmark|bookmarks)',
    re.I)


def _metrics_from_group(art):
    """Exact counts from the action bar's aria-label.

    Reads "12 replies, 3 reposts, 45 likes, 1200 views", which is both cheaper
    and more accurate than scraping each button: the visible label says "1.2K"
    where the aria-label says 1234.
    """
    # Counts are suffixed _count so they cannot collide with ctx['replies'],
    # which is the list of reply texts. An earlier cut of this file had the
    # metric named 'replies' and ctx.update() quietly replaced the list with an
    # integer, which only surfaced as an AttributeError on .append().
    out = {'reply_count': 0, 'repost_count': 0, 'like_count': 0, 'view_count': 0}
    try:
        label = art.locator('[role="group"]').first.get_attribute('aria-label', timeout=3000)
    except Exception:
        return out
    if not label:
        return out
    for num, word in _METRIC_RE.findall(label):
        w = word.lower()
        key = ('reply_count' if w.startswith('repl') else
               'repost_count' if w.startswith('repost') else
               'like_count' if w.startswith('like') else
               'view_count' if w.startswith('view') else None)
        if key:
            out[key] = _parse_count(num)
    return out


def _tweet_text(art):
    """The post body, all of it, with the quoted tweet excluded.

    A quote tweet nests the quoted post's tweetText inside the outer article, so
    a naive all_inner_texts() concatenates someone else's words onto the author's
    and the reply ends up answering the wrong person. The quoted block sits under
    a div[role="link"]; take the first tweetText that is not inside one.
    """
    try:
        nodes = art.locator('[data-testid="tweetText"]')
        n = nodes.count()
    except Exception:
        return ''
    for i in range(min(n, 4)):
        node = nodes.nth(i)
        try:
            inside_quote = node.evaluate(
                'el => !!el.closest(\'div[role="link"]\')')
        except Exception:
            inside_quote = False
        if inside_quote:
            continue
        try:
            return node.inner_text(timeout=3000).strip()
        except Exception:
            continue
    return ''


def _quoted_text(art):
    try:
        q = art.locator('div[role="link"] [data-testid="tweetText"]').first
        if q.count():
            return q.inner_text(timeout=2000).strip()
    except Exception:
        pass
    return ''


def _image_alts(art):
    """Alt text on attached images.

    Most posts have none, but a screenshot of a benchmark or a stack trace is a
    very common shape for exactly the technical posts worth replying to, and
    without this the model sees a one line post with no visible subject.
    """
    alts = []
    try:
        imgs = art.locator('[data-testid="tweetPhoto"] img')
        for i in range(min(imgs.count(), 4)):
            a = imgs.nth(i).get_attribute('alt', timeout=1500) or ''
            a = a.strip()
            # X uses the literal string "Image" as the default alt.
            if a and a.lower() not in ('image', 'photo'):
                alts.append(a)
    except Exception:
        pass
    return alts


def _link_card(art):
    try:
        card = art.locator('[data-testid="card.wrapper"]').first
        if card.count():
            txt = card.inner_text(timeout=2000).strip()
            return re.sub(r'\s*\n\s*', ' | ', txt)[:300]
    except Exception:
        pass
    return ''


def _author_handle(art):
    try:
        name = art.locator('[data-testid="User-Name"]').first.inner_text(timeout=3000)
    except Exception:
        return '', ''
    display, handle = '', ''
    for line in name.split('\n'):
        line = line.strip()
        if line.startswith('@') and not handle:
            handle = line.lstrip('@')
        elif line and not display and not line.startswith('@') and line != '·':
            display = line
    return display, handle


def _age_hours(art):
    try:
        dt = art.locator('time').first.get_attribute('datetime', timeout=3000)
        posted = datetime.fromisoformat(dt.replace('Z', '+00:00'))
        return max(0.0, (datetime.now(timezone.utc) - posted).total_seconds() / 3600.0)
    except Exception:
        return None


def fetch_context(page, tweet_url, want_replies=4, want_author=True):
    """Open a tweet's permalink and read everything that bears on a reply.

    Returns None when the page will not load or the focal tweet cannot be
    identified, which callers must treat as "skip this candidate": drafting from
    a half read page is how the old card-text pipeline went wrong in the first
    place.
    """
    tid = ''
    m = re.search(r'/status/(\d+)', tweet_url or '')
    if m:
        tid = m.group(1)
    if not tid:
        return None

    try:
        page.goto(tweet_url, timeout=40000, wait_until='domcontentloaded')
    except Exception as e:
        print(f'[ctx] load failed {tweet_url}: {e}', file=sys.stderr)
        return None
    page.wait_for_timeout(1200)
    try:
        page.wait_for_selector('article[data-testid="tweet"]', timeout=15000)
    except Exception:
        print(f'[ctx] no article rendered on {tweet_url}', file=sys.stderr)
        return None
    # Let the reply list paint. Replies arrive after the focal tweet and a
    # fixed sleep here is worth more than a selector wait, because "no replies
    # yet" and "replies still loading" look identical in the DOM.
    page.wait_for_timeout(2500)

    articles = page.locator('article[data-testid="tweet"]').all()
    if not articles:
        return None

    # DOM order on a status page is [thread ancestors..., focal, replies...].
    # The focal article is the one carrying a permalink to this status id.
    focal_idx = None
    for i, art in enumerate(articles):
        try:
            if art.locator(f'a[href*="/status/{tid}"]').count():
                focal_idx = i
                break
        except Exception:
            continue
    if focal_idx is None:
        # A single-tweet page with an unusual header still has exactly one
        # article; falling back to it beats discarding a good candidate.
        focal_idx = 0

    focal = articles[focal_idx]
    display, handle = _author_handle(focal)
    body = _tweet_text(focal)
    metrics = _metrics_from_group(focal)

    ctx = {
        'url': tweet_url,
        'tweet_id': tid,
        'author': handle,
        'author_display': display,
        'text': body,
        'quoted': _quoted_text(focal),
        'images': _image_alts(focal),
        'link_card': _link_card(focal),
        'parent': '',
        'parent_author': '',
        'replies': [],
        'age_hours': _age_hours(focal),
        'is_thread_start': False,
    }
    ctx.update(metrics)

    # The tweet this one is answering. Replying to a reply without reading the
    # parent is how you confidently answer a question nobody asked.
    if focal_idx > 0:
        parent = articles[focal_idx - 1]
        p_display, p_handle = _author_handle(parent)
        ctx['parent'] = _tweet_text(parent)
        ctx['parent_author'] = p_handle or p_display
        # A self-reply is the author continuing their own thread, so the two
        # posts are one piece of writing and should be read as such.
        if p_handle and handle and p_handle.lower() == handle.lower():
            ctx['is_thread_start'] = True

    # What has already been said. The single most useful field for reach: a
    # point that four people have already made earns nothing, and the model
    # cannot avoid repeating an argument it has never seen.
    for art in articles[focal_idx + 1: focal_idx + 1 + want_replies]:
        r_display, r_handle = _author_handle(art)
        r_text = _tweet_text(art)
        if r_text:
            ctx['replies'].append({'author': r_handle or r_display, 'text': r_text[:400]})

    if want_author and handle:
        info = fetch_author(page, handle)
        if info:
            ctx['author_bio'] = info.get('bio', '')
            ctx['author_followers'] = info.get('followers', 0)
            ctx['author_unknown'] = info.get('unknown', False)

    return ctx


def render_for_model(ctx):
    """Flatten a context dict into the block the drafting prompt receives.

    Plain labelled lines, because that is what the small model handles most
    reliably; JSON in the user turn made flash-lite answer in JSON.
    """
    parts = []
    who = f"@{ctx.get('author', '')}"
    bio = (ctx.get('author_bio') or '').replace('\n', ' ').strip()
    if bio:
        who += f" — bio: {bio[:200]}"
    followers = ctx.get('author_followers') or 0
    if followers:
        who += f" ({followers:,} followers)"
    parts.append(f"Post author: {who}")

    if ctx.get('parent'):
        rel = ('Earlier post in the same thread by the same author'
               if ctx.get('is_thread_start') else
               f"This post is a reply to @{ctx.get('parent_author', '')}, who wrote")
        parts.append(f"{rel}:\n{ctx['parent'][:900]}")

    parts.append(f"The post:\n{(ctx.get('text') or '')[:2000]}")

    if ctx.get('quoted'):
        parts.append(f"It quotes this other post:\n{ctx['quoted'][:700]}")
    if ctx.get('images'):
        parts.append('Attached image described as: ' + ' / '.join(ctx['images'])[:400])
    if ctx.get('link_card'):
        parts.append(f"Linked article: {ctx['link_card']}")

    if ctx.get('replies'):
        said = '\n'.join(f"  @{r['author']}: {r['text'][:220]}" for r in ctx['replies'])
        parts.append("Replies already posted under it. Do not repeat any of these "
                     f"points:\n{said}")
    else:
        parts.append("No replies under it yet.")

    age = ctx.get('age_hours')
    if age is not None:
        parts.append(f"Posted {age:.1f} hours ago, {ctx.get('like_count', 0)} likes, "
                     f"{ctx.get('reply_count', 0)} replies.")
    return '\n\n'.join(parts)


if __name__ == '__main__':
    # Manual probe: x_context.py <tweet-url>
    import argparse
    from playwright.sync_api import sync_playwright

    ap = argparse.ArgumentParser()
    ap.add_argument('url')
    a = ap.parse_args()

    init_tables()
    profile = os.path.expanduser('~/.hermes/data/chrome_automation_profile')
    with sync_playwright() as p:
        c = p.chromium.launch_persistent_context(
            user_data_dir=profile, channel='chrome', headless=True,
            args=['--disable-blink-features=AutomationControlled', '--no-sandbox'])
        pg = c.new_page()
        pg.set_viewport_size({'width': 1440, 'height': 1000})
        ctx = fetch_context(pg, a.url)
        c.close()
    print(json.dumps(ctx, indent=2)[:4000])
    print('\n--- as the model sees it ---\n')
    print(render_for_model(ctx) if ctx else '(no context)')
