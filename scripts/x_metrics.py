#!/usr/bin/env python3
"""Daily metrics snapshot for @VishalPanwarr.

Records the six numbers that X_GROWTH_PLAN.md section 11 says to steer by, so
the daily review job has something to compare against instead of guessing.

The counts that matter most (inbound replies, engagement by tier) come from
x_growth.db, which every social script already writes to. Only the follower and
following counts need a browser, and if that fails the run still records
everything else — a missing follower count is not worth losing a day of data
over.

    x_metrics.py            snapshot + print today vs 7 days ago
    x_metrics.py --no-browser   skip the profile scrape
    x_metrics.py --report       print only, record nothing
"""
import os
import sys
import json
import sqlite3
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

GROWTH_DB = os.path.expanduser('~/.hermes/data/x_growth.db')
METRICS_DB = os.path.expanduser('~/.hermes/data/x_metrics.db')
TARGETS = os.path.expanduser('~/Development/Aarambh/hermes-jobs/x_targets.json')
HANDLE = 'vishalpanwarr'


def init_db():
    conn = sqlite3.connect(METRICS_DB)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS daily_metrics (
            day               TEXT PRIMARY KEY,
            followers         INTEGER,
            following         INTEGER,
            replies_sent      INTEGER,
            inbound_replies   INTEGER,
            likes_given       INTEGER,
            follows_made      INTEGER,
            posts_published   INTEGER,
            tier_hit_rate     REAL,
            captured_at       TEXT
        )
    ''')
    # Every like and follow the engage script performs, so hit rate and
    # follow-back can be computed per tier later.
    conn.execute('''
        CREATE TABLE IF NOT EXISTS engagement_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            action     TEXT,
            handle     TEXT,
            tier       TEXT,
            url        TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_eng_created ON engagement_log(created_at)')
    conn.commit()
    return conn


def load_target_handles():
    """Flat set of every handle on the target list, lowercased, plus tier map."""
    tiers = {}
    try:
        data = json.load(open(TARGETS))
    except Exception:
        return tiers
    for key, val in data.items():
        if not key.startswith('tier_') or not isinstance(val, list):
            continue
        for entry in val:
            h = (entry.get('handle') or '').lstrip('@').lower()
            if h:
                tiers[h] = key
    return tiers


def db_counts(day):
    """Counts sourced from whatever the social scripts already recorded.

    x_growth.db writes created_at via CURRENT_TIMESTAMP, which SQLite stores in
    UTC. Comparing that against a local calendar date silently drops everything
    posted between midnight and 05:30 IST into the previous day, so the
    'localtime' modifier here is load-bearing, not decoration.
    """
    out = {'replies_sent': 0, 'inbound_replies': 0, 'tier_hit_rate': 0.0}
    if not os.path.exists(GROWTH_DB):
        return out
    LOCAL_DAY = "date(created_at, 'localtime')"
    conn = sqlite3.connect(GROWTH_DB)
    try:
        out['replies_sent'] = conn.execute(
            f"SELECT COUNT(*) FROM auto_replies WHERE {LOCAL_DAY} = ?", (day,)).fetchone()[0]
        out['inbound_replies'] = conn.execute(
            f"SELECT COUNT(*) FROM inbound_replies WHERE {LOCAL_DAY} = ?", (day,)).fetchone()[0]

        # The number the plan actually steers by: what fraction of today's
        # replies went to someone we chose on purpose. 4% historically.
        tiers = load_target_handles()
        if tiers:
            authors = [r[0] for r in conn.execute(
                f"SELECT author FROM auto_replies WHERE {LOCAL_DAY} = ?", (day,)) if r[0]]
            if authors:
                hits = sum(1 for a in authors if a.lstrip('@').lower() in tiers)
                out['tier_hit_rate'] = round(hits / len(authors), 3)
    except sqlite3.Error as e:
        print(f"[metrics] growth db read failed: {e}", file=sys.stderr)
    finally:
        conn.close()
    return out


def engagement_counts(conn, day):
    rows = conn.execute(
        "SELECT action, COUNT(*) FROM engagement_log WHERE date(created_at) = ? GROUP BY action",
        (day,)).fetchall()
    d = dict(rows)
    return {'likes_given': d.get('like', 0), 'follows_made': d.get('follow', 0)}


def posts_today(day):
    hist = os.path.expanduser('~/.hermes/scripts/x_post_history.json')
    try:
        data = json.load(open(hist))
    except Exception:
        return 0
    return sum(1 for p in data.get('posts', []) if str(p.get('date', ''))[:10] == day)


def scrape_profile():
    """Follower/following counts. Returns (followers, following) or (None, None).

    Deliberately best-effort: X changes this markup often and a failure here
    must not cost us the rest of the snapshot.
    """
    try:
        from playwright.sync_api import sync_playwright
        from browser_lock import profile_lock, BrowserBusy
    except ImportError as e:
        print(f"[metrics] browser deps unavailable: {e}", file=sys.stderr)
        return None, None

    profile = os.path.expanduser('~/.hermes/data/chrome_automation_profile')
    try:
        with profile_lock('x_metrics'):
            with sync_playwright() as p:
                ctx = p.chromium.launch_persistent_context(
                    user_data_dir=profile, channel='chrome', headless=True,
                    args=['--disable-blink-features=AutomationControlled', '--no-sandbox'])
                page = ctx.new_page()
                page.set_viewport_size({'width': 1440, 'height': 900})
                page.goto(f'https://x.com/{HANDLE}', timeout=45000,
                          wait_until='domcontentloaded')
                page.wait_for_timeout(4000)
                if any(x in page.url.lower() for x in ('login', 'i/flow', 'authorize')):
                    print('[metrics] not logged in — skipping profile scrape', file=sys.stderr)
                    ctx.close()
                    return None, None

                def count(kind):
                    for sel in (f'a[href="/{HANDLE}/{kind}"] span span',
                                f'a[href$="/{kind}"] span span'):
                        try:
                            txt = page.locator(sel).first.inner_text(timeout=4000)
                            return parse_count(txt)
                        except Exception:
                            continue
                    return None

                followers = count('verified_followers') or count('followers')
                following = count('following')
                ctx.close()
                return followers, following
    except BrowserBusy:
        print('[metrics] chrome profile busy — skipping profile scrape', file=sys.stderr)
    except Exception as e:
        print(f'[metrics] profile scrape failed: {e}', file=sys.stderr)
    return None, None


def parse_count(text):
    t = (text or '').strip().replace(',', '').upper()
    if not t:
        return None
    mult = 1
    if t.endswith('K'):
        mult, t = 1_000, t[:-1]
    elif t.endswith('M'):
        mult, t = 1_000_000, t[:-1]
    try:
        return int(float(t) * mult)
    except ValueError:
        return None


def snapshot(use_browser=True):
    day = datetime.now().strftime('%Y-%m-%d')
    conn = init_db()
    row = {'day': day}
    row.update(db_counts(day))
    row.update(engagement_counts(conn, day))
    row['posts_published'] = posts_today(day)
    row['followers'], row['following'] = (scrape_profile() if use_browser else (None, None))

    # Never overwrite a real follower count with a failed scrape.
    prev = conn.execute('SELECT followers, following FROM daily_metrics WHERE day = ?',
                        (day,)).fetchone()
    if prev:
        row['followers'] = row['followers'] if row['followers'] is not None else prev[0]
        row['following'] = row['following'] if row['following'] is not None else prev[1]

    row['captured_at'] = datetime.now().isoformat(timespec='seconds')
    conn.execute('''
        INSERT INTO daily_metrics
            (day, followers, following, replies_sent, inbound_replies,
             likes_given, follows_made, posts_published, tier_hit_rate, captured_at)
        VALUES (:day, :followers, :following, :replies_sent, :inbound_replies,
                :likes_given, :follows_made, :posts_published, :tier_hit_rate, :captured_at)
        ON CONFLICT(day) DO UPDATE SET
            followers=excluded.followers, following=excluded.following,
            replies_sent=excluded.replies_sent, inbound_replies=excluded.inbound_replies,
            likes_given=excluded.likes_given, follows_made=excluded.follows_made,
            posts_published=excluded.posts_published, tier_hit_rate=excluded.tier_hit_rate,
            captured_at=excluded.captured_at
    ''', row)
    conn.commit()
    conn.close()
    return row


CANDIDATES = os.path.expanduser('~/Development/Aarambh/hermes-jobs/x_target_candidates.json')
LIST_GOAL = 150
LIKES_PER_ACCOUNT_PER_WEEK = 5   # mirrors x_target_engage.MAX_LIKES_PER_ACCOUNT_PER_WEEK


def list_status(conn):
    """The state of the target list, counted by code rather than by the agent.

    The 2026-09-09 review reported "7 handles" against a file holding 4, and
    read every zero as the engine being blocked. Both were countable. This puts
    the counts the review needs in the report it already reads, so the model has
    nothing left to estimate: how big the list is per tier, how many candidates
    are waiting for a human, and whether the like budget is exhausted by the
    per-account weekly cap rather than by anything being broken.
    """
    tiers = load_target_handles()
    per_tier = {}
    for h, t in tiers.items():
        per_tier[t] = per_tier.get(t, 0) + 1
    short = {'tier_1_inner': 't1', 'tier_2_founders': 't2', 'tier_3_vc': 't3', 'tier_4_peers': 't4'}
    tier_txt = ', '.join(f"{short.get(k, k)} {per_tier.get(k, 0)}" for k in short)

    pending = 0
    try:
        cands = json.load(open(CANDIDATES)).get('candidates', [])
        pending = sum(1 for c in cands if str(c.get('status', '')).startswith('candidate'))
    except Exception:
        pass

    since = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
    capped = 0
    try:
        rows = dict(conn.execute(
            "SELECT lower(handle), COUNT(*) FROM engagement_log "
            "WHERE action='like' AND date(created_at) >= ? GROUP BY lower(handle)",
            (since,)).fetchall())
        capped = sum(1 for h in tiers if rows.get(h, 0) >= LIKES_PER_ACCOUNT_PER_WEEK)
    except sqlite3.Error:
        pass

    lines = [
        f"  target list      {len(tiers)}/{LIST_GOAL}  ({tier_txt})",
        f"  candidates       {pending} awaiting human review in x_target_candidates.json",
    ]
    if tiers:
        lines.append(f"  like cap         {capped}/{len(tiers)} handles at "
                     f"{LIKES_PER_ACCOUNT_PER_WEEK} likes this week"
                     + ("   <- likes are cap-bound, not broken" if capped == len(tiers) else ''))
    return lines


def report():
    """Today live, plus the trailing 7-day totals. Printed for the agent.

    Today's row is recomputed from source rather than read back from
    daily_metrics. The stored row is only written by the 23:45 snapshot, so
    reading it at 08:00 hands the daily review a row that is nearly a day stale
    and reads as zeros — which is exactly what happened on the first morning:
    the review concluded nothing had run while four replies sat in the database.
    History still comes from the table; only today is recomputed.
    """
    if not os.path.exists(METRICS_DB):
        return 'No metrics recorded yet.'
    conn = sqlite3.connect(METRICS_DB)
    conn.row_factory = sqlite3.Row
    today = datetime.now().strftime('%Y-%m-%d')
    week_ago = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')

    stored = conn.execute('SELECT * FROM daily_metrics WHERE day = ?', (today,)).fetchone()
    cur = dict(stored) if stored else {'followers': None, 'following': None}
    cur.update(db_counts(today))
    cur.update(engagement_counts(conn, today))
    cur['posts_published'] = posts_today(today)
    week = conn.execute('''
        SELECT SUM(replies_sent) r, SUM(inbound_replies) i, SUM(likes_given) l,
               SUM(follows_made) f, SUM(posts_published) p, AVG(tier_hit_rate) t,
               COUNT(*) days
        FROM daily_metrics WHERE day >= ?''', (week_ago,)).fetchone()
    first = conn.execute('''
        SELECT followers FROM daily_metrics
        WHERE day >= ? AND followers IS NOT NULL ORDER BY day LIMIT 1''', (week_ago,)).fetchone()

    lines = [f'X metrics — {today}', '']
    if cur:
        # 0% with zero replies is not a hit rate, it is a division the code
        # skipped. Printing it as 0% made the review report a failing metric on
        # days the reply engine correctly stayed quiet.
        hit = (f"{cur['tier_hit_rate']:.0%}" if cur['replies_sent'] else 'n/a (no replies)')
        lines += [
            f"  followers        {cur['followers'] if cur['followers'] is not None else 'n/a'}"
            + (f"  ({cur['followers'] - first['followers']:+d} in 7d)"
               if cur['followers'] is not None and first and first['followers'] is not None else ''),
            f"  replies sent     {cur['replies_sent']}",
            f"  inbound replies  {cur['inbound_replies']}   <- north star",
            f"  likes given      {cur['likes_given']}",
            f"  follows made     {cur['follows_made']}",
            f"  posts published  {cur['posts_published']}",
            f"  tier hit rate    {hit}   <- share of replies aimed at the target list",
        ]
    lines += ['', *list_status(conn)]
    lines += ['', f"  7-day totals ({week['days']} days recorded)"]
    if week and week['days']:
        lines += [
            f"    replies {week['r'] or 0}   inbound {week['i'] or 0}   likes {week['l'] or 0}"
            f"   follows {week['f'] or 0}   posts {week['p'] or 0}",
            f"    avg tier hit rate {(week['t'] or 0):.0%}",
        ]
    conn.close()
    return '\n'.join(lines)


if __name__ == '__main__':
    if '--report' not in sys.argv:
        snapshot(use_browser='--no-browser' not in sys.argv)
    print(report())
