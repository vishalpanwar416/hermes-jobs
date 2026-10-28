#!/usr/bin/env python3
"""Reply to technical posts on X, for reach.

Driven by the `social_x_reply_outbound` cron every 90 minutes.

Rewritten 2026-09-16. The previous version did one thing per run: search
`from:` over the five handles in x_targets.json inside a two hour window. Between
09-10 and 09-15 that search came back empty on 75 consecutive runs. Zero replies,
zero reach, six days, and nothing in the logs that looked like a failure, because
an empty target window is a legitimate quiet run (X_GROWTH_PLAN.md section 5).
The job was not broken. It was pointed at a five handle list through a two hour
keyhole, and the list is a human gate that code is not allowed to widen.

Three changes, in the order they matter:

1. More sources for posts, none for handles. x_discover pulls from the target
   list (window now 6h), the home timeline, and a topic search. The target list
   is still the only thing that decides whose posts are privileged; the other two
   only add posts to consider, and every non-target author has to clear a
   follower floor before a reply is spent on them.

2. The post is read before it is answered. x_context opens the permalink and
   returns the untruncated body, the thread parent, the quoted tweet, image alt
   text, the author's bio and follower count, and the replies already posted
   underneath. Every reply this system has ever drafted was drafted from a
   truncated feed card instead, which is both why it misfired under @wagslane's
   joke and why the model kept declining perfectly good posts: four visible lines
   really do contain nothing to engage with.

3. Ranking on reach rather than on keywords. score_post() weighs follower count,
   how old the post is and how many replies are already under it, because those
   decide whether a reply is seen. A flawless reply on a twelve hour old post
   with ninety replies is invisible.

The topic source is on by default here, reversing the 2026-09-07 decision that
turned it off. That decision was right about the old implementation, which scored
a truncated card against a keyword list and posted at a 4% hit rate. It is worth
being explicit that this is not the same thing being switched back on: a topic
post now has to clear the card filter, a 1,200 follower floor on its author, a
reply saturation check, a reach score, the model's own SKIP, and judge_relevance
at 4/5 — six gates, four of which read the actual post. `--sources targets` is
still one flag away if the replies it produces do not hold up.

    auto_reply_growth.py                  live, up to 2 replies
    auto_reply_growth.py --dry-run        find, read, draft, post nothing
    auto_reply_growth.py --sources targets,feed
    auto_reply_growth.py 3 --min-score 20
"""
import os
import sys
import time
import json
import random
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if __name__ == '__main__':
    # Only when run directly. Firing on import made every script that
    # imports this module log its own run under this pipeline's name.
    try:
        import pipeline_log as _plog
        _plog.start('social_x_reply_outbound')
    except Exception:
        pass

import social_voice as voice
import x_context
import x_discover
from browser_lock import profile_lock, BrowserBusy, clear_chrome_singletons

DB_PATH = os.path.expanduser('~/.hermes/data/x_growth.db')
AUTOMATION_PROFILE_DIR = os.path.expanduser('~/.hermes/data/chrome_automation_profile')

# The job runs every 90 minutes so it can catch a target's post while it is
# still fresh, which means up to 16 runs a day. Without a daily ceiling that
# turns 2 replies a run into 30+ replies a day, which is the exact volume the
# plan is trying to get away from. Frequency buys timing, not quantity.
DAILY_REPLY_CAP = 12

# Never reply twice to the same account inside this window. Section 5: three
# replies to one handle in two days reads as stalking.
AUTHOR_COOLDOWN_HOURS = 48

# Two floors on x_discover.score_post(), because the two kinds of candidate are
# not competing for the same thing. A target-list post is worth answering for the
# relationship even when its reach is poor, so it only has to clear a low bar. A
# stranger's post is worth answering only for reach, so it has to be good.
#
# 26 rather than the 30 this started at, set from the first live shortlist
# (2026-09-16) rather than from invented examples. At 30 the run's single best
# candidate was thrown away before it was ever drafted: a MinIO explainer from a
# 44,800 follower account, ten hours old with four replies under it, which scored
# 28.2 because the score docks a post for modest like counts and for being past
# the eight hour mark. Drafted by hand afterwards it produced a reply about
# erasure coding costs that judge_relevance scored 5/5. A floor that rejects that
# post is measuring the wrong thing.
#
# Being a little permissive here is the right bias now that the score is only
# deciding where to spend page loads and replies. The two gates that decide
# whether anything is actually posted are downstream and much sharper: the
# model's own SKIP, and judge_relevance at 4/5.
MIN_SCORE_TARGET = 12.0
MIN_SCORE_STRANGER = 26.0

# Opening a permalink costs a page load and the author profile costs another, so
# the shortlist is walked in rank order and abandoned once enough replies are
# posted or this many posts have been read.
MAX_CONTEXT_FETCHES = 6


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------

def init_replies_table():
    conn = sqlite3.connect(DB_PATH)
    conn.execute('''
    CREATE TABLE IF NOT EXISTS auto_replies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tweet_id TEXT UNIQUE NOT NULL,
        author TEXT,
        tweet_text TEXT,
        reply_text TEXT,
        status TEXT DEFAULT 'posted',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    conn.commit()
    conn.close()
    x_context.init_tables()


def is_tweet_replied(tweet_id):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT 1 FROM auto_replies WHERE tweet_id = ?",
                       (tweet_id,)).fetchone()
    conn.close()
    return row is not None


def is_url_replied(url):
    import re
    m = re.search(r'/status/(\d+)', url or '')
    return bool(m) and is_tweet_replied(m.group(1))


def has_used_recent_reply(reply_text, limit=10):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT reply_text FROM auto_replies ORDER BY id DESC LIMIT ?",
                        (limit,)).fetchall()
    conn.close()
    return any(r[0] == reply_text for r in rows)


def is_author_recently_replied(author, hours=AUTHOR_COOLDOWN_HOURS):
    """Have we already replied to this account inside the cooldown window?

    This used to look at the last N rows instead of a time window. At low volume
    those coincide; the moment throughput rises, the window slides past the
    duplicate before it can be caught, which is how kmeanskaran and
    TanzilaSha9574 each collected three replies inside two days. A fixed row
    count is a time window pretending to be one.
    """
    if not author:
        return False
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT 1 FROM auto_replies WHERE lower(author) = lower(?) "
                       "AND created_at >= datetime('now', ?) LIMIT 1",
                       (author, f'-{int(hours)} hours')).fetchone()
    conn.close()
    return row is not None


def record_reply(tweet_id, author, tweet_text, reply_text, status='posted'):
    conn = sqlite3.connect(DB_PATH)
    conn.execute('INSERT OR IGNORE INTO auto_replies '
                 '(tweet_id, author, tweet_text, reply_text, status) VALUES (?,?,?,?,?)',
                 (tweet_id, author, (tweet_text or '')[:500], reply_text, status))
    conn.commit()
    conn.close()


def replies_sent_today():
    """Rolling 24 hours, not a calendar day.

    created_at is stored in UTC by CURRENT_TIMESTAMP, so comparing it to a local
    calendar date makes the cap forget everything sent between midnight and
    05:30 IST — which would hand the job a fresh budget of 12 in the middle of
    the night. A rolling window has no boundary to fall through and is the
    better rate limit anyway.
    """
    conn = sqlite3.connect(DB_PATH)
    try:
        return conn.execute("SELECT COUNT(*) FROM auto_replies WHERE status='posted' "
                            "AND created_at >= datetime('now', '-24 hours')").fetchone()[0]
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# drafting
# ---------------------------------------------------------------------------

def draft_for(ctx):
    """Draft a reply to a fetched context, or return None to post nothing.

    Three gates, in increasing cost: the model's own SKIP, a repeat check against
    what was recently sent, and judge_relevance() on the pair. The judge is not
    optional and an unscorable pair is dropped — an unverified reply to a
    hand-picked target is the one outcome worse than silence (section 5).
    """
    body = (ctx.get('text') or '').strip()
    if not body:
        return None

    block = x_context.render_for_model(ctx)
    drafted = voice.draft_comment_ctx(block, body, platform='x')

    if drafted is voice.SKIP:
        print(f"[voice] declined @{ctx.get('author')}", file=sys.stderr)
        return None
    if not drafted:
        print(f"[voice] no draft for @{ctx.get('author')} (model call failed) — "
              f"posting nothing", file=sys.stderr)
        return None
    if has_used_recent_reply(drafted):
        print(f"[voice] draft for @{ctx.get('author')} repeats a recent reply — dropped",
              file=sys.stderr)
        return None

    # The judge scores against the full post, which is the version the reply was
    # actually written for. Scoring it against the old truncated card would have
    # penalised a reply for answering a sentence the card cut off.
    score = voice.judge_relevance(body, drafted)
    if score is None:
        print(f"[judge] could not score reply to @{ctx.get('author')} — posting nothing",
              file=sys.stderr)
        return None
    if score < voice.RELEVANCE_MIN:
        print(f"[judge] {score}/5 for @{ctx.get('author')}, below {voice.RELEVANCE_MIN} "
              f"— dropped:\n        post:  {body[:110]!r}\n        draft: {drafted[:110]!r}",
              file=sys.stderr)
        return None

    print(f"[judge] {score}/5 for @{ctx.get('author')}", file=sys.stderr)
    return drafted


def post_reply(page, url, text):
    """Post one reply on the permalink page. True when it went out."""
    try:
        page.goto(url, timeout=40000, wait_until='domcontentloaded')
        page.wait_for_timeout(3000)

        btn = page.locator('[data-testid="reply"]').first
        if not btn.count() or not btn.is_visible():
            print(f'[reply] no reply button on {url}', file=sys.stderr)
            return False
        btn.click()
        page.wait_for_timeout(2000)

        editor = page.locator('div[data-testid="tweetTextarea_0"]').first
        if not editor.count() or not editor.is_visible():
            print(f'[reply] no composer opened on {url}', file=sys.stderr)
            return False
        editor.click()
        page.wait_for_timeout(400)
        page.keyboard.type(text, delay=10)
        page.wait_for_timeout(1500)

        send = page.locator('[data-testid="tweetButton"]').first
        if not send.count() or not send.is_enabled():
            print(f'[reply] send button not enabled on {url}', file=sys.stderr)
            return False
        send.click()
        page.wait_for_timeout(6000)
        return True
    except Exception as e:
        print(f'[reply] failed on {url}: {e}', file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------

def scan_and_reply(max_replies=2, dry_run=False, sources=('targets', 'feed', 'topic'),
                   min_score=MIN_SCORE_STRANGER):
    init_replies_table()
    done = []

    sent = replies_sent_today()
    if sent >= DAILY_REPLY_CAP:
        print(f'[cap] {sent}/{DAILY_REPLY_CAP} replies in the last 24h — stopping',
              file=sys.stderr)
        print('[SILENT] daily reply cap reached')
        return []
    max_replies = min(max_replies, DAILY_REPLY_CAP - sent)

    # Safe here and only here: the caller holds the profile lock, so there is no
    # live Chrome whose singletons these could be. Doing it before acquiring the
    # lock is what let one job delete another job's lock mid-run.
    clear_chrome_singletons()

    from playwright.sync_api import sync_playwright

    considered = 0
    rejected = []
    with sync_playwright() as pw:
        ctx_browser = None
        try:
            ctx_browser = pw.chromium.launch_persistent_context(
                user_data_dir=AUTOMATION_PROFILE_DIR, channel='chrome', headless=True,
                args=['--disable-blink-features=AutomationControlled', '--no-sandbox',
                      '--disable-dev-shm-usage', '--disable-gpu'])
            page = ctx_browser.new_page()
            page.set_viewport_size({'width': 1440, 'height': 1000})

            shortlist = x_discover.discover(
                page, sources=sources, shortlist=8,
                skip_url=is_url_replied, skip_author=is_author_recently_replied)

            if not shortlist:
                print('[reply] nothing passed the card filter this run', file=sys.stderr)
                print('[SILENT] no candidate posts found')
                return []

            for cand in shortlist:
                if len(done) >= max_replies or considered >= MAX_CONTEXT_FETCHES:
                    break
                considered += 1

                ctx = x_context.fetch_context(page, cand['url'])
                if not ctx:
                    rejected.append((cand['author'], 'context unreadable'))
                    continue

                score, reason = x_discover.score_post(
                    ctx, is_target=cand.get('is_target', False),
                    card_score=cand.get('card_score', 0))
                tag = 'TARGET' if cand.get('is_target') else cand['source']
                if score is None:
                    print(f'  [skip] @{ctx.get("author")} ({tag}): {reason}', file=sys.stderr)
                    rejected.append((ctx.get('author'), reason))
                    continue
                floor = MIN_SCORE_TARGET if cand.get('is_target') else min_score
                print(f'  [{score}/{floor}] @{ctx.get("author")} ({tag}): {reason}',
                      file=sys.stderr)
                if score < floor:
                    rejected.append((ctx.get('author'), f'score {score} below {floor}'))
                    continue

                # The author can differ from the card's guess once the post is
                # open (a repost, or a link scraped off the wrong anchor), so the
                # cooldown is re-checked against the real handle.
                if is_author_recently_replied(ctx.get('author')):
                    rejected.append((ctx.get('author'), 'replied within cooldown'))
                    continue

                reply = draft_for(ctx)
                if not reply:
                    rejected.append((ctx.get('author'), 'no usable draft'))
                    continue

                print(f'\n@{ctx.get("author")} ({tag}, score {score})\n'
                      f'  post:  {(ctx.get("text") or "")[:160]}\n'
                      f'  reply: {reply}', file=sys.stderr)

                if dry_run:
                    done.append({'author': ctx.get('author'), 'url': ctx['url'],
                                 'source': cand['source'], 'score': score,
                                 'post': (ctx.get('text') or '')[:200],
                                 'reply': reply, 'status': 'dry-run'})
                    continue

                if post_reply(page, ctx['url'], reply):
                    record_reply(ctx['tweet_id'], ctx.get('author'),
                                 ctx.get('text'), reply)
                    done.append({'author': ctx.get('author'), 'url': ctx['url'],
                                 'source': cand['source'], 'score': score,
                                 'post': (ctx.get('text') or '')[:200],
                                 'reply': reply, 'status': 'posted'})
                    print(f'[reply] posted #{len(done)} to @{ctx.get("author")}',
                          file=sys.stderr)
                    time.sleep(random.uniform(4, 9))
                else:
                    record_reply(ctx['tweet_id'], ctx.get('author'), ctx.get('text'),
                                 reply, status='failed')
                    rejected.append((ctx.get('author'), 'post failed'))

        except Exception as e:
            print(f'[reply] run error: {e}', file=sys.stderr)
        finally:
            if ctx_browser is not None:
                try:
                    ctx_browser.close()
                except Exception:
                    pass

    if not done:
        # Why nothing happened, when nothing happens. A bare [SILENT] is what
        # made six days of empty runs look like six days of a quiet timeline.
        detail = '; '.join(f'@{a}: {r}' for a, r in rejected[:4]) or 'no candidates'
        print(f'[SILENT] {considered} posts read, none replied to ({detail})')
    else:
        print(f'[reply] {len(done)} replies this run '
              f'({sent + len(done)}/{DAILY_REPLY_CAP} in 24h)')
    return done


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description='Reply to high-reach technical posts on X.')
    # Positional count kept so the existing cron invocation still works.
    # 2 per run against a 12/day cap spreads the budget across the whole day
    # instead of spending it in the first four runs before anyone is awake.
    ap.add_argument('max_replies', nargs='?', type=int, default=2,
                    help='maximum replies to post in one run (default 2)')
    ap.add_argument('--dry-run', action='store_true',
                    help='discover, read, draft and judge, but post nothing')
    ap.add_argument('--sources', default='targets,feed,topic',
                    help='comma separated: targets, feed, topic (default all three)')
    ap.add_argument('--min-score', type=float, default=MIN_SCORE_STRANGER,
                    help=f'reach score floor for a non-target post '
                         f'(default {MIN_SCORE_STRANGER}; target-list posts always '
                         f'use {MIN_SCORE_TARGET})')
    args = ap.parse_args()

    srcs = tuple(s.strip() for s in args.sources.split(',') if s.strip())
    try:
        with profile_lock('social_x_reply_outbound'):
            out = scan_and_reply(max_replies=args.max_replies, dry_run=args.dry_run,
                                 sources=srcs, min_score=args.min_score)
    except BrowserBusy as e:
        print(f'Skipping run: {e}', file=sys.stderr)
        print('[SILENT] browser busy')
        out = []
    print(json.dumps(out, indent=2))
