#!/usr/bin/env python3
"""Likes and follows against the X target list.

X_GROWTH_PLAN.md sections 6 and 7. Two levers that were entirely unused: a like
costs nothing and a follow is a free notification, and both are how a target
comes to recognise the handle before it ever replies to them. The plan's
sequencing rule is the point — like two or three of someone's posts across a few
days, then follow, and only then reply. A reply from a cold handle reads as
spam; from a familiar one it reads as a peer.

Two floors, because the two actions differ in risk. Likes need 3 handles, follows
need 10: following three seed accounts eight times a day is not a strategy, but
liking their posts under a per-account weekly cap is exactly the warm-up the plan
asks for, and there is no reason to hold it back while the list fills.

    x_target_engage.py              one pass, respecting daily caps
    x_target_engage.py --dry-run    decide and print, touch nothing
    x_target_engage.py --likes-only skip follows (week 2 of the rollout)
"""
import os
import re
import sys
import json
import time
import random
import sqlite3
import argparse
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TARGETS = os.path.expanduser('~/Development/Aarambh/hermes-jobs/x_targets.json')
METRICS_DB = os.path.expanduser('~/.hermes/data/x_metrics.db')
PROFILE = os.path.expanduser('~/.hermes/data/chrome_automation_profile')

# Daily ceilings from the plan. X starts flagging above roughly 25 follows/day,
# so 18 leaves headroom for the reply job's own activity.
DAILY_LIKES = 35
DAILY_FOLLOWS = 18
# Spread across ~8 runs a day.
PER_RUN_LIKES = 6
PER_RUN_FOLLOWS = 3
# Section 6: never look like you're camped on one account.
MAX_LIKES_PER_ACCOUNT_PER_WEEK = 5
# Two floors, not one — the actions carry different risk. Liking a handful of
# posts from hand-picked accounts, capped at 5 per account per week, cannot look
# like spam however short the list is, and the warm-up it buys is the whole
# reason a later reply reads as a peer rather than a stranger. Following is the
# action with a ceiling worth respecting, so it waits for a real list.
MIN_LIST_LIKES = 3
MIN_LIST_FOLLOWS = 10


def load_targets():
    try:
        data = json.load(open(TARGETS))
    except Exception as e:
        print(f'[engage] cannot read target list: {e}', file=sys.stderr)
        return []
    out = []
    for key, val in data.items():
        if not key.startswith('tier_') or not isinstance(val, list):
            continue
        for entry in val:
            h = (entry.get('handle') or '').lstrip('@')
            if h:
                out.append({'handle': h, 'tier': key})
    return out


def db():
    conn = sqlite3.connect(METRICS_DB)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS engagement_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT, handle TEXT, tier TEXT, url TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )''')
    conn.commit()
    return conn


def counts_today(conn):
    day = datetime.now().strftime('%Y-%m-%d')
    rows = dict(conn.execute(
        "SELECT action, COUNT(*) FROM engagement_log WHERE date(created_at)=? GROUP BY action",
        (day,)).fetchall())
    return rows.get('like', 0), rows.get('follow', 0)


def already_followed(conn, handle):
    return conn.execute(
        "SELECT 1 FROM engagement_log WHERE action='follow' AND lower(handle)=? LIMIT 1",
        (handle.lower(),)).fetchone() is not None


def likes_this_week(conn, handle):
    since = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
    return conn.execute(
        "SELECT COUNT(*) FROM engagement_log WHERE action='like' AND lower(handle)=? "
        "AND date(created_at) >= ?", (handle.lower(), since)).fetchone()[0]


def log(conn, action, handle, tier, url=''):
    conn.execute('INSERT INTO engagement_log (action, handle, tier, url, created_at) '
                 'VALUES (?,?,?,?,?)',
                 (action, handle, tier, url, datetime.now().isoformat(timespec='seconds')))
    conn.commit()


def run(dry_run=False, likes_only=False):
    targets = load_targets()
    if len(targets) < MIN_LIST_LIKES:
        print(f'[engage] target list has {len(targets)} accounts, need at least '
              f'{MIN_LIST_LIKES} to like anything. Populate x_targets.json (run '
              f'x_prospect.py, then promote candidates by hand). Doing nothing.',
              file=sys.stderr)
        print('[SILENT] target list too small')
        return
    if len(targets) < MIN_LIST_FOLLOWS and not likes_only:
        print(f'[engage] {len(targets)} accounts — below the {MIN_LIST_FOLLOWS} needed '
              f'to follow. Liking only; the warm-up still runs.', file=sys.stderr)
        likes_only = True

    conn = db()
    likes_done, follows_done = counts_today(conn)
    like_budget = min(PER_RUN_LIKES, max(0, DAILY_LIKES - likes_done))
    follow_budget = 0 if likes_only else min(PER_RUN_FOLLOWS,
                                             max(0, DAILY_FOLLOWS - follows_done))
    if like_budget == 0 and follow_budget == 0:
        print(f'[engage] daily caps reached ({likes_done} likes, {follows_done} follows)')
        print('[SILENT] caps reached')
        return

    # Tier 1 and 2 first — they are the accounts worth being recognised by.
    order = {'tier_1_inner': 0, 'tier_2_founders': 1, 'tier_3_vc': 2, 'tier_4_peers': 3}
    random.shuffle(targets)
    targets.sort(key=lambda t: order.get(t['tier'], 9))

    from playwright.sync_api import sync_playwright
    from browser_lock import profile_lock, BrowserBusy

    liked = followed = 0
    # Why nothing happened, when nothing happens. The daily review reads this
    # output and "[SILENT] nothing to do" made it conclude the engine was
    # blocked, when on a 4-handle list the truth was that every handle had
    # already collected its 5 likes for the week — a cap doing its job.
    capped = [t['handle'] for t in targets
              if likes_this_week(conn, t['handle']) >= MAX_LIKES_PER_ACCOUNT_PER_WEEK]
    try:
        with profile_lock('x_target_engage'):
            with sync_playwright() as p:
                ctx = p.chromium.launch_persistent_context(
                    user_data_dir=PROFILE, channel='chrome', headless=True,
                    args=['--disable-blink-features=AutomationControlled', '--no-sandbox'])
                page = ctx.new_page()
                page.set_viewport_size({'width': 1440, 'height': 1000})
                page.goto('https://x.com/home', timeout=45000, wait_until='domcontentloaded')
                page.wait_for_timeout(3000)
                if any(x in page.url.lower() for x in ('login', 'i/flow', 'authorize')):
                    print('[engage] not logged in — aborting', file=sys.stderr)
                    ctx.close()
                    return

                for t in targets:
                    if liked >= like_budget and followed >= follow_budget:
                        break
                    handle, tier = t['handle'], t['tier']
                    if likes_this_week(conn, handle) >= MAX_LIKES_PER_ACCOUNT_PER_WEEK:
                        continue
                    try:
                        page.goto(f'https://x.com/{handle}', timeout=30000,
                                  wait_until='domcontentloaded')
                        page.wait_for_timeout(3000)

                        # Follow first: it is the notification that introduces you.
                        if followed < follow_budget and not already_followed(conn, handle):
                            btn = page.locator(f'[data-testid$="-follow"]').first
                            if btn.count() and btn.is_visible():
                                if dry_run:
                                    print(f'  would follow @{handle} ({tier})')
                                else:
                                    btn.click()
                                    page.wait_for_timeout(1500)
                                    log(conn, 'follow', handle, tier)
                                    print(f'  followed @{handle} ({tier})')
                                followed += 1
                                time.sleep(random.uniform(2, 4))

                        # Then like a couple of recent posts.
                        if liked < like_budget:
                            arts = page.locator('article')
                            n = min(arts.count(), 4)
                            for i in range(n):
                                if liked >= like_budget:
                                    break
                                if likes_this_week(conn, handle) + 1 > MAX_LIKES_PER_ACCOUNT_PER_WEEK:
                                    break
                                art = arts.nth(i)
                                # The first article on a profile is usually the
                                # pinned post, often months old, and reposts
                                # belong to someone else: liking either spends
                                # one of the five weekly likes without putting
                                # anything in the target's notifications.
                                try:
                                    ctx_txt = art.locator(
                                        '[data-testid="socialContext"]').first.inner_text(
                                        timeout=800).lower()
                                except Exception:
                                    ctx_txt = ''
                                if 'pinned' in ctx_txt or 'repost' in ctx_txt:
                                    continue
                                like_btn = art.locator('[data-testid="like"]').first
                                if not like_btn.count():
                                    continue
                                url = ''
                                try:
                                    href = art.locator('a[href*="/status/"]').first.get_attribute(
                                        'href', timeout=2000)
                                    url = ('https://x.com' + href) if href and href.startswith('/') else (href or '')
                                except Exception:
                                    pass
                                if dry_run:
                                    print(f'  would like {url or handle}')
                                else:
                                    try:
                                        like_btn.click(timeout=4000)
                                    except Exception:
                                        continue
                                    page.wait_for_timeout(900)
                                    log(conn, 'like', handle, tier, url)
                                    print(f'  liked @{handle} {url}')
                                liked += 1
                                time.sleep(random.uniform(2.5, 5))
                    except Exception as e:
                        print(f'  ! @{handle}: {e}', file=sys.stderr)
                        continue

                ctx.close()
    except BrowserBusy:
        print('[engage] chrome profile busy — will retry next run', file=sys.stderr)
        print('[SILENT] browser busy')
        return

    conn.close()
    if liked or followed:
        print(f'[engage] {liked} likes, {followed} follows this run '
              f'(day total {likes_done + liked}/{DAILY_LIKES} likes, '
              f'{follows_done + followed}/{DAILY_FOLLOWS} follows)')
        return
    reasons = []
    if capped:
        reasons.append(f'{len(capped)}/{len(targets)} handles at the weekly like cap '
                       f'({MAX_LIKES_PER_ACCOUNT_PER_WEEK}/wk)')
    if likes_only and len(targets) < MIN_LIST_FOLLOWS:
        reasons.append(f'follows held until the list reaches {MIN_LIST_FOLLOWS} '
                       f'(now {len(targets)})')
    if not reasons:
        reasons.append('no likeable fresh posts found')
    print('[SILENT] ' + '; '.join(reasons))


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--likes-only', action='store_true')
    a = ap.parse_args()
    run(dry_run=a.dry_run, likes_only=a.likes_only)
