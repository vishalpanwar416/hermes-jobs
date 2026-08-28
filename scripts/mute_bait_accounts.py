"""Mute the X accounts that repeatedly post job bait.

The feed monitors classify every hiring post and log a strike against any
account whose post is rejected (see job_filter.record_rejection). This script
acts on repeat offenders so the junk stops reaching the feed in the first place.

Deliberate choices:

MUTE, NOT BLOCK, by default. Muting is reversible, invisible to the other
person, and costs nothing if the classifier got it wrong. Blocking is visible,
socially loaded, and occasionally lands on a real recruiter who wrote a lazy
post. Blocking requires an explicit --block.

STRIKES, NOT ONE STRIKE. A single bad post is not a pattern. The default
threshold is 3 rejected posts.

NEVER TOUCH ACCOUNTS YOU FOLLOW. If Vishal follows them, that is a deliberate
signal that outranks the heuristic. Checked live on the profile page.

ALLOWLIST WINS. Anything marked allowlisted is never actioned.

Usage:
    python mute_bait_accounts.py --dry-run        # show what it would mute
    python mute_bait_accounts.py                  # mute repeat offenders
    python mute_bait_accounts.py --block          # block instead of mute
    python mute_bait_accounts.py --list           # show the ledger
    python mute_bait_accounts.py --allow user1 user2   # never action these
    python mute_bait_accounts.py --forgive user1       # clear strikes
"""

import os
import sys
import time
import json
import sqlite3
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# Capture this run's full stdout and stderr to
# ~/.hermes/logs/pipelines/social_bait_muter/ so a failed or silent run can be
# diagnosed afterwards instead of vanishing.
if __name__ == '__main__':
    # Only when run directly. Firing on import made every script that
    # imports this module log its own run under this pipeline's name.
    try:
        import pipeline_log as _plog
        _plog.start('social_bait_muter')
    except Exception:
        pass

from job_filter import _bait_conn
from browser_lock import profile_lock, BrowserBusy

from playwright.sync_api import sync_playwright

DEFAULT_THRESHOLD = 3
MAX_ACTIONS_PER_RUN = 5  # stay well under anything that looks automated


def candidates(threshold, platform='x'):
    conn = _bait_conn()
    try:
        return conn.execute(
            '''SELECT author, strikes, last_reason, last_url FROM bait_accounts
               WHERE platform = ? AND strikes >= ? AND allowlisted = 0
                 AND muted_at IS NULL AND blocked_at IS NULL
               ORDER BY strikes DESC''',
            (platform, threshold)).fetchall()
    finally:
        conn.close()


def mark(author, field, platform='x'):
    conn = _bait_conn()
    try:
        conn.execute(
            f"UPDATE bait_accounts SET {field} = CURRENT_TIMESTAMP "
            "WHERE platform = ? AND author = ?", (platform, author))
        conn.commit()
    finally:
        conn.close()


def set_allow(authors, value=1, platform='x'):
    conn = _bait_conn()
    try:
        for a in authors:
            conn.execute(
                '''INSERT INTO bait_accounts (platform, author, allowlisted)
                   VALUES (?, ?, ?)
                   ON CONFLICT(platform, author)
                   DO UPDATE SET allowlisted = excluded.allowlisted''',
                (platform, a.lstrip('@'), value))
        conn.commit()
    finally:
        conn.close()


def forgive(authors, platform='x'):
    conn = _bait_conn()
    try:
        for a in authors:
            conn.execute(
                "UPDATE bait_accounts SET strikes = 0, muted_at = NULL, "
                "blocked_at = NULL WHERE platform = ? AND author = ?",
                (platform, a.lstrip('@')))
        conn.commit()
    finally:
        conn.close()


def show_ledger():
    conn = _bait_conn()
    try:
        rows = conn.execute(
            '''SELECT platform, author, strikes, muted_at, blocked_at,
                      allowlisted, last_reason
               FROM bait_accounts ORDER BY strikes DESC LIMIT 60''').fetchall()
    finally:
        conn.close()
    if not rows:
        print("Ledger is empty. It fills as the feed monitors reject posts.")
        return
    print(f"{'plat':9s} {'account':26s} {'hits':>4s}  state       reason")
    for plat, author, strikes, muted, blocked, allow, reason in rows:
        state = ('allowlisted' if allow else
                 'blocked' if blocked else 'muted' if muted else '-')
        print(f"{plat:9s} {author[:26]:26s} {strikes:>4d}  {state:11s} {(reason or '')[:44]}")


def _follows(page, author):
    """Does Vishal follow this account? A follow outranks the heuristic."""
    try:
        page.goto(f"https://x.com/{author}", timeout=35000,
                  wait_until="domcontentloaded")
        time.sleep(3)
        # X labels the button "Following" only when you follow them.
        btn = page.locator('[data-testid$="-unfollow"], button[aria-label^="Following"]').first
        return bool(btn.count())
    except Exception as e:
        print(f"  ! could not check follow state for @{author}: {e}", file=sys.stderr)
        return True  # fail safe: treat as followed, do nothing


def _action_via_menu(page, author, block=False):
    """Mute or block from the profile's overflow menu."""
    want = 'Block' if block else 'Mute'
    try:
        more = page.locator('[data-testid="userActions"]').first
        if not more.count():
            more = page.locator('button[aria-label^="More"]').first
        if not more.count():
            return False, 'overflow menu not found'
        more.click()
        # The menu animates in; 1.5s was short enough that the items were not
        # queryable yet and a present "Mute" looked missing.
        page.wait_for_selector('[role="menuitem"]', timeout=8000)
        time.sleep(0.8)

        # X labels mute as a bare "Mute" but block as "Block @handle", so match
        # on the item text rather than assuming either shape.
        item = None
        for el in page.locator('[role="menuitem"]').all():
            label = (el.inner_text() or '').strip().split('\n')[0]
            if label.lower().startswith(want.lower()):
                item = el
                break
        if item is None:
            labels = [(e.inner_text() or '').strip().split('\n')[0]
                      for e in page.locator('[role="menuitem"]').all()]
            page.keyboard.press('Escape')
            return False, f'"{want}" not in menu (saw: {labels})'
        item.click()
        time.sleep(1.5)

        # Block asks for confirmation; mute usually does not.
        confirm = page.locator('[data-testid="confirmationSheetConfirm"]').first
        if confirm.count() and confirm.is_visible():
            confirm.click()
            time.sleep(2)
        return True, 'ok'
    except Exception as e:
        return False, str(e)[:90]


def _linkedin_action(page, name, block=False):
    """Hide a LinkedIn account's posts from the feed.

    LinkedIn has no per-account mute for feed posts, so the equivalent is
    Unfollow (keeps the connection, stops the posts). 'Block' is offered but is
    a much heavier action there than on X, so it stays behind --block.

    Feed authors are display names, not handles, so this searches People rather
    than guessing a profile URL.
    """
    try:
        page.goto('https://www.linkedin.com/search/results/people/?keywords='
                  + name.replace(' ', '%20'),
                  timeout=40000, wait_until='domcontentloaded')
        time.sleep(4)
        result = page.locator('a[href*="/in/"]').first
        if not result.count():
            return False, 'no profile found for that name'
        href = (result.get_attribute('href') or '').split('?')[0]
        page.goto(href, timeout=40000, wait_until='domcontentloaded')
        time.sleep(4)

        if block:
            more = page.locator('button:has-text("More")').first
            if not more.count():
                return False, 'More menu not found'
            more.click(); time.sleep(1.5)
            item = page.locator('div[role="button"]:has-text("Report / Block"), '
                                'span:has-text("Report / Block")').first
            if not item.count():
                page.keyboard.press('Escape')
                return False, 'block option not found'
            # Deliberately stops here: LinkedIn's block flow needs a multi-step
            # confirmation and misfiring it is not recoverable quietly.
            page.keyboard.press('Escape')
            return False, 'block requires manual confirmation on LinkedIn'

        following = page.locator('button:has-text("Following")').first
        if following.count() and following.is_visible():
            following.click(); time.sleep(1.5)
            confirm = page.locator('button:has-text("Unfollow")').first
            if confirm.count() and confirm.is_visible():
                confirm.click(); time.sleep(2)
            return True, 'unfollowed'

        more = page.locator('button:has-text("More")').first
        if more.count():
            more.click(); time.sleep(1.5)
            unf = page.locator('div[role="button"]:has-text("Unfollow"), '
                               'span:has-text("Unfollow")').first
            if unf.count() and unf.is_visible():
                unf.click(); time.sleep(2)
                return True, 'unfollowed'
            page.keyboard.press('Escape')
        return False, 'not following, nothing to do'
    except Exception as e:
        return False, str(e)[:90]


def run_linkedin(threshold=DEFAULT_THRESHOLD, dry_run=False, block=False,
                 limit=MAX_ACTIONS_PER_RUN):
    targets = candidates(threshold, platform='linkedin')
    if not targets:
        return []
    if dry_run:
        return [{'author': a, 'strikes': s, 'reason': r,
                 'action': 'would unfollow (linkedin)'} for a, s, r, _ in targets[:limit]]

    done = []
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=os.path.expanduser('~/.hermes/data/chrome_automation_profile'),
            channel="chrome", headless=True,
            args=['--disable-blink-features=AutomationControlled', '--no-sandbox'])
        page = context.new_page()
        page.set_viewport_size({"width": 1440, "height": 900})
        for name, strikes, reason, url in targets[:limit]:
            ok, detail = _linkedin_action(page, name, block=block)
            if ok:
                mark(name, 'blocked_at' if block else 'muted_at', platform='linkedin')
                done.append({'author': name, 'strikes': strikes, 'reason': reason,
                             'action': detail, 'platform': 'linkedin'})
                print(f"  {detail} {name} ({strikes} strikes)", file=sys.stderr)
            else:
                print(f"  skipped {name}: {detail}", file=sys.stderr)
            time.sleep(3)
        context.close()
    return done


def run(threshold=DEFAULT_THRESHOLD, dry_run=False, block=False, limit=MAX_ACTIONS_PER_RUN):
    targets = candidates(threshold)
    if not targets:
        print("[SILENT] No accounts over the strike threshold.")
        return []

    verb = 'block' if block else 'mute'
    print(f"{len(targets)} account(s) at or over {threshold} strikes.", file=sys.stderr)

    if dry_run:
        out = []
        for author, strikes, reason, url in targets[:limit]:
            print(f"  [dry-run] would {verb} @{author} ({strikes} strikes): {reason}")
            out.append({'author': author, 'strikes': strikes, 'reason': reason,
                        'action': f'would {verb}'})
        return out

    done = []
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=os.path.expanduser('~/.hermes/data/chrome_automation_profile'),
            channel="chrome", headless=True,
            args=['--disable-blink-features=AutomationControlled', '--no-sandbox'])
        page = context.new_page()
        page.set_viewport_size({"width": 1440, "height": 900})

        for author, strikes, reason, url in targets[:limit]:
            if _follows(page, author):
                print(f"  skipping @{author}: you follow them", file=sys.stderr)
                set_allow([author])  # stop reconsidering them every run
                continue

            ok, detail = _action_via_menu(page, author, block=block)
            if ok:
                mark(author, 'blocked_at' if block else 'muted_at')
                done.append({'author': author, 'strikes': strikes,
                             'reason': reason, 'action': verb + 'd',
                             'example': url})
                print(f"  {verb}d @{author} ({strikes} strikes)", file=sys.stderr)
            else:
                print(f"  failed to {verb} @{author}: {detail}", file=sys.stderr)
            time.sleep(3)

        context.close()
    return done


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--block', action='store_true',
                    help='block instead of mute (visible to them, use sparingly)')
    ap.add_argument('--threshold', type=int, default=DEFAULT_THRESHOLD)
    ap.add_argument('--limit', type=int, default=MAX_ACTIONS_PER_RUN)
    ap.add_argument('--platform', choices=['x', 'linkedin', 'both'], default='both',
                    help='which platform to act on (default both)')
    ap.add_argument('--list', action='store_true', help='show the ledger and exit')
    ap.add_argument('--allow', nargs='+', metavar='USER', help='never action these accounts')
    ap.add_argument('--forgive', nargs='+', metavar='USER', help='reset strikes')
    args = ap.parse_args()

    if args.list:
        show_ledger(); sys.exit(0)
    if args.allow:
        set_allow(args.allow); print(f"allowlisted: {', '.join(args.allow)}"); sys.exit(0)
    if args.forgive:
        forgive(args.forgive); print(f"strikes cleared: {', '.join(args.forgive)}"); sys.exit(0)

    if args.dry_run:
        out = run(args.threshold, dry_run=True, block=args.block, limit=args.limit)
        if args.platform in ('linkedin', 'both'):
            out += run_linkedin(args.threshold, dry_run=True, block=args.block,
                                limit=args.limit)
        print(json.dumps(out, indent=2))
    else:
        try:
            # One lock for the whole run: both platforms drive the same profile.
            with profile_lock('mute_bait_accounts'):
                done = []
                if args.platform in ('x', 'both'):
                    done += run(args.threshold, block=args.block, limit=args.limit)
                if args.platform in ('linkedin', 'both'):
                    done += run_linkedin(args.threshold, block=args.block,
                                         limit=args.limit)
        except BrowserBusy as e:
            print(f"Skipping run: {e}", file=sys.stderr)
            done = []
        print(json.dumps(done, indent=2))
