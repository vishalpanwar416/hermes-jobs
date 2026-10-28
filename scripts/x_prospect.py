#!/usr/bin/env python3
"""Find real candidates for the X target list.

X_GROWTH_PLAN.md section 3 needs ~150 deliberately chosen accounts. Inventing
handles would be worse than useless — you'd follow and reply to people who
either don't exist or aren't who you think — so this harvests them from two
places where the right people demonstrably already are.

*Who a seed follows.* A respected infra account's following list is a hand-curated
set of people that account thinks are worth reading. This is the higher-quality
source and it is where tier 1, 2 and 3 candidates actually live.

*Who replies to a seed.* Anyone who writes a substantive reply to a respected
account is in the conversation you want to be in. It is a broader net and a
noisier one — structurally a tier-4 pool — so it runs second and contributes
fewer profile checks.

Both feed the same filter: follower range, bio keywords, no engagement-farm
tells. Survivors go to x_target_candidates.json for human review. It never edits
x_targets.json — promotion to the real list is a decision, not a scrape.

    x_prospect.py                harvest, write candidates
    x_prospect.py --seeds 5      how many tier-1 seeds to walk (default 3)
    x_prospect.py --no-following  skip the following lists, replies only
    x_prospect.py --dry-run      print, write nothing
"""
import os
import re
import sys
import json
import argparse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TARGETS = os.path.expanduser('~/Development/Aarambh/hermes-jobs/x_targets.json')
CANDIDATES = os.path.expanduser('~/Development/Aarambh/hermes-jobs/x_target_candidates.json')
PROFILE = os.path.expanduser('~/.hermes/data/chrome_automation_profile')
OWN_HANDLE = 'vishalpanwarr'

# Bio signals, by the tier the account would land in.
TIER_SIGNALS = {
    'tier_2_founders': [
        'founder', 'co-founder', 'cofounder', 'building ', 'ceo', 'cto',
        'yc ', 'y combinator', 'shipping', 'we build',
    ],
    'tier_3_vc': [
        'partner', 'investor', 'vc', 'seed', 'venture', 'angel', 'invests',
        'backing', 'early stage',
    ],
    'tier_1_inner': [
        'core team', 'maintainer', 'creator of', 'author of', 'staff engineer',
        'principal engineer', 'distinguished',
    ],
}

# Must look like an engineer of some kind, whatever tier they land in.
TECH_SIGNALS = [
    'go', 'golang', 'rust', 'backend', 'infra', 'infrastructure', 'distributed',
    'kubernetes', 'k8s', 'devops', 'sre', 'platform', 'database', 'postgres',
    'redis', 'kafka', 'ai', 'ml', 'llm', 'rag', 'agents', 'inference',
    'developer', 'engineer', 'systems', 'dev tools', 'devtools', 'open source',
    'software', 'programmer', 'swe', 'eng', 'coding', 'cto', 'hacker',
]

# Below 500 they can't help; above 500k they'll never see you.
MIN_FOLLOWERS = 500
MAX_FOLLOWERS = 500_000

FARM_SIGNALS = [
    'follow for follow', 'f4f', 'dm for promo', 'link in bio', 'grow your account',
    'i help you', 'dm me to', 'content creator', 'growth hacker',
]

# The 09-06 harvest was three students and a VC. The follower floor catches
# most of that now, but a bio that says so outright should not need a count.
JUNIOR_SIGNALS = [
    'student', 'fresher', 'intern ', 'internship', 'undergrad', 'learning to code',
    'bootcamp', 'final year', 'final yr', 'sophomore', 'freshman',
    'aspiring developer', 'aspiring engineer', 'aspiring software', 'aspiring dev',
    # Not 'aspiring' or 'enthusiast' on their own: a Menlo Ventures partner's
    # bio ends "aspiring comedian", and "AI enthusiast" is on plenty of staff
    # engineers' profiles too. The follower floor handles the rest.
]

# Company and org accounts. Every tier in the plan is a person — a founder, a
# partner, a peer — and a brand account neither follows back nor replies as
# one. These are the unambiguous tells; softer ones ("we", "our") only earn a
# note, because founders write "we're building" too.
ORG_SIGNALS = [
    'official account', 'official twitter', 'official x account', 'product updates',
    'need support?', 'customer support', 'sign up', 'get started', 'start free',
    'for developers creating', 'the open platform', 'knowledge sharing &',
    'views are our own', 'careers:', 'we are hiring', "we're hiring",
]
ORG_SOFT_SIGNALS = ['we ', "we're", 'our ', 'us ']

# X's own routes, which match the same /handle shape as a profile link.
NAV_PATHS = {'home', 'explore', 'notifications', 'messages', 'i', 'search',
             'settings', 'compose', 'following', 'followers', 'bookmarks', 'jobs'}

# How many profiles to open per run. Profile checks are the slow part, so the
# budget is split rather than raised — following lists are the better source and
# get the larger half.
CHECK_BUDGET_FOLLOWING = 25
CHECK_BUDGET_REPLIES = 15


def load_json(path, default):
    try:
        return json.load(open(path))
    except Exception:
        return default


def existing_handles():
    """Every handle this run must not spend a profile check on.

    Targets, exclusions, and — since 2026-09-10 — everything already sitting in
    the candidates file, whether it was kept or rejected. Without the last part
    the daily run re-opened the same 25 profiles from the top of @wagslane's
    following list every morning, re-derived the same candidates, and wrote
    "1 new candidate". A daily cadence buys nothing if the budget is spent on
    handles that were checked yesterday.
    """
    data = load_json(TARGETS, {})
    out = set()
    for key, val in data.items():
        if key.startswith('tier_') and isinstance(val, list):
            out |= {(e.get('handle') or '').lstrip('@').lower() for e in val}
    excl = data.get('_excluded', {}).get('handles', [])
    out |= {h.lstrip('@').lower() for h in excl}
    cands = load_json(CANDIDATES, {})
    out |= {(c.get('handle') or '').lower() for c in cands.get('candidates', [])}
    out |= {h.lower() for h in cands.get('_rejected', {})}
    out.add(OWN_HANDLE)
    out.discard('')
    return out


def seeds(limit):
    """Accounts whose following lists and repliers are worth walking.

    Tier 1 first, then tiers 2 and 3. A VC's following list is where the other
    VCs and the founders they back live; walking only the three tier-1 seeds
    kept producing DevRel and education accounts, because that is who a course
    founder follows.
    """
    data = load_json(TARGETS, {})
    out = []
    for key in ('tier_1_inner', 'tier_2_founders', 'tier_3_vc'):
        for e in data.get(key, []):
            h = (e.get('handle') or '').lstrip('@')
            if h and h not in out:
                out.append(h)
    return out[:limit]


def _tier_signal(bio_lower):
    for tier, signals in TIER_SIGNALS.items():
        if any(s in bio_lower for s in signals):
            return tier
    return None


def _mentions_tech(bio_lower):
    return any(re.search(rf'(?<![a-z0-9]){re.escape(t)}s?(?![a-z0-9])', bio_lower)
               for t in TECH_SIGNALS)


def reject_reason(bio, followers):
    """Why a profile should not even reach the candidates file, or None.

    The tech-keyword requirement is waived when the bio carries a founder, VC or
    maintainer signal. The first dry run of the wider walk (2026-09-10) threw
    out "founder of @browserbase", "building @ferrymanio, prev eng @google" and
    "Formerly founder @join_taro" as no_tech — exactly the tier-2 people the
    list is short of, lost to terse bios. A human reviews the file anyway; the
    follower range still applies.
    """
    b = (bio or '').lower()
    if any(f in b for f in FARM_SIGNALS):
        return 'farm'
    if any(f in b for f in JUNIOR_SIGNALS):
        return 'junior'
    if any(f in b for f in ORG_SIGNALS):
        return 'org'
    if followers is not None and not (MIN_FOLLOWERS <= followers <= MAX_FOLLOWERS):
        return 'followers_out_of_range'
    if not _mentions_tech(b) and not _tier_signal(b):
        return 'no_tech'
    return None


def classify(bio):
    b = (bio or '').lower()
    if any(f in b for f in FARM_SIGNALS):
        return None
    tier = _tier_signal(b)
    if tier:
        return tier
    if not _mentions_tech(b):
        return None
    return 'tier_4_peers'


def soft_notes(bio):
    b = (bio or '').lower()
    notes = []
    if any(s in b for s in ORG_SOFT_SIGNALS) or b.startswith(('the ', 'a ', 'an ')):
        notes.append('reads like a company account')
    if any(s in b for s in ('devrel', 'developer advocate', 'developer relations',
                            'educator', 'youtube', 'course', 'teacher', 'podcast')):
        notes.append('educator/devrel, not a founder or peer')
    return '; '.join(notes)


def parse_count(text):
    t = (text or '').strip().replace(',', '').upper()
    m = re.match(r'^([\d.]+)([KM]?)$', t)
    if not m:
        return None
    n = float(m.group(1))
    return int(n * {'': 1, 'K': 1_000, 'M': 1_000_000}[m.group(2)])


def harvest(seed_limit, dry_run, with_following=True):
    from playwright.sync_api import sync_playwright
    from browser_lock import profile_lock

    seed_handles = seeds(seed_limit)
    if not seed_handles:
        print('[prospect] no tier-1 seeds in x_targets.json — add some first', file=sys.stderr)
        return []

    known = existing_handles()
    found = {}

    with profile_lock('x_prospect'):
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=PROFILE, channel='chrome', headless=True,
                args=['--disable-blink-features=AutomationControlled', '--no-sandbox'])
            page = ctx.new_page()
            page.set_viewport_size({'width': 1440, 'height': 1200})

            page.goto('https://x.com/home', timeout=45000, wait_until='domcontentloaded')
            page.wait_for_timeout(3000)
            if any(x in page.url.lower() for x in ('login', 'i/flow', 'authorize')):
                print('[prospect] not logged in — aborting', file=sys.stderr)
                ctx.close()
                return []

            # 1. Who each seed follows. Curated by someone whose judgement you
            #    already trust, which is why it runs first and gets the bigger
            #    share of the profile-check budget.
            if with_following:
                for seed in seed_handles:
                    print(f'[prospect] following list of @{seed}', file=sys.stderr)
                    try:
                        page.goto(f'https://x.com/{seed}/following', timeout=45000,
                                  wait_until='domcontentloaded')
                        page.wait_for_timeout(3500)
                        if '/following' not in page.url:
                            print(f'  ! @{seed} following list not visible', file=sys.stderr)
                            continue
                        seen = 0
                        for _ in range(8):
                            links = page.eval_on_selector_all(
                                '[data-testid="UserCell"] a[href^="/"]',
                                'els => els.map(e => e.getAttribute("href"))')
                            for h in links or []:
                                m = re.fullmatch(r'/([A-Za-z0-9_]{2,15})', h or '')
                                if not m:
                                    continue
                                handle = m.group(1).lower()
                                if handle in known or handle in found or handle in NAV_PATHS:
                                    continue
                                found[handle] = {'handle': handle,
                                                 'source': f'followed by @{seed}'}
                            if len(found) == seen:
                                break        # list exhausted, stop scrolling
                            seen = len(found)
                            page.mouse.wheel(0, 2400)
                            page.wait_for_timeout(1400)
                    except Exception as e:
                        print(f'[prospect] @{seed} following failed: {e}', file=sys.stderr)

            from_following = set(found)
            print(f'[prospect] {len(from_following)} handles from following lists',
                  file=sys.stderr)

            # 2. Collect handles replying under each seed's recent posts.
            for seed in seed_handles:
                print(f'[prospect] walking @{seed}', file=sys.stderr)
                try:
                    page.goto(f'https://x.com/{seed}', timeout=45000,
                              wait_until='domcontentloaded')
                    page.wait_for_timeout(3500)
                    hrefs = page.eval_on_selector_all(
                        'a[href*="/status/"]', 'els => els.map(e => e.getAttribute("href"))')
                    posts = []
                    for h in hrefs or []:
                        if h and '/status/' in h and '/analytics' not in h:
                            url = 'https://x.com' + h if h.startswith('/') else h
                            url = url.split('/photo/')[0]
                            if url not in posts:
                                posts.append(url)
                    for post_url in posts[:2]:
                        page.goto(post_url, timeout=45000, wait_until='domcontentloaded')
                        page.wait_for_timeout(3500)
                        for _ in range(3):
                            page.mouse.wheel(0, 2000)
                            page.wait_for_timeout(1200)
                        links = page.eval_on_selector_all(
                            'article a[href^="/"]',
                            'els => els.map(e => e.getAttribute("href"))')
                        for h in links or []:
                            m = re.fullmatch(r'/([A-Za-z0-9_]{2,15})', h or '')
                            if not m:
                                continue
                            handle = m.group(1).lower()
                            if handle in known or handle in found or handle == seed.lower():
                                continue
                            if handle in NAV_PATHS:
                                continue
                            found[handle] = {'handle': handle, 'source': f'replies to @{seed}'}
                except Exception as e:
                    print(f'[prospect] @{seed} failed: {e}', file=sys.stderr)

            # Check the better source first and give it the larger budget.
            queue = ([h for h in found if h in from_following][:CHECK_BUDGET_FOLLOWING]
                     + [h for h in found if h not in from_following][:CHECK_BUDGET_REPLIES])
            print(f'[prospect] {len(found)} raw handles, checking {len(queue)} profiles',
                  file=sys.stderr)

            # 3. Check each profile for bio, follower count, and tier fit.
            kept = []
            rejected = {}
            for handle in queue:
                try:
                    page.goto(f'https://x.com/{handle}', timeout=30000,
                              wait_until='domcontentloaded')
                    page.wait_for_timeout(2500)
                    try:
                        bio = page.locator('[data-testid="UserDescription"]').first.inner_text(
                            timeout=3000)
                    except Exception:
                        bio = ''
                    # Match on the href suffix, not the full path. X preserves the
                    # display case of a handle in its URLs and everything here is
                    # lowercased, so an exact-href selector silently matched nothing
                    # and every candidate came back with followers=None — which the
                    # range check below then waved through.
                    try:
                        fol_txt = page.locator(
                            'a[href$="/verified_followers"] span span, '
                            'a[href$="/followers"] span span').first.inner_text(timeout=3000)
                        followers = parse_count(fol_txt)
                    except Exception:
                        followers = None

                    why = reject_reason(bio, followers)
                    tier = classify(bio) if not why else None
                    if not why and followers is None and tier == 'tier_4_peers':
                        # Unknown reach and no tier-1/2/3 signal in the bio is the
                        # exact profile of the students this harvest kept last run.
                        # Where the bio earns a real tier, a human can judge it.
                        why = 'no_count_peer'
                    if why or not tier:
                        rejected[handle] = {'reason': why or 'no_tier',
                                            'followers': followers,
                                            'bio': ' '.join((bio or '').split())[:100],
                                            'at': datetime.now().strftime('%Y-%m-%d')}
                        print(f'  - @{handle}  {why or "no_tier"}  {followers}', file=sys.stderr)
                        continue
                    # Recent topics, so promotion is a decision on evidence rather
                    # than a second visit to the profile.
                    try:
                        arts = page.locator('article')
                        recent = ' | '.join(
                            ' '.join(arts.nth(i).inner_text(timeout=2000).split())[:90]
                            for i in range(min(arts.count(), 2)))
                    except Exception:
                        recent = ''
                    entry = {
                        'handle': handle,
                        'suggested_tier': tier,
                        'followers': followers,
                        'bio': ' '.join((bio or '').split())[:180],
                        'recent': recent[:220],
                        'source': found[handle]['source'],
                        'status': 'candidate',
                    }
                    note = soft_notes(bio)
                    if note:
                        entry['note'] = note
                    kept.append(entry)
                    print(f'  + @{handle}  {tier}  {followers}'
                          + (f'  [{note}]' if note else ''), file=sys.stderr)
                except Exception as e:
                    print(f'  ! @{handle}: {e}', file=sys.stderr)

            ctx.close()

    if dry_run:
        print(json.dumps({'kept': kept, 'rejected': rejected}, indent=2))
        return kept

    existing = load_json(CANDIDATES, {'candidates': []})
    have = {c['handle'] for c in existing.get('candidates', [])}
    new = [c for c in kept if c['handle'] not in have]
    existing.setdefault('candidates', []).extend(new)
    # Remember the rejects too, so tomorrow's budget goes to profiles nobody
    # has looked at yet. Kept small: reason, count, a bio stub and the date.
    existing.setdefault('_rejected', {}).update(rejected)
    existing['_comment'] = ('Harvested candidates for x_targets.json. Review, then move the '
                            'good ones into the matching tier by hand. Nothing here is followed '
                            'or replied to until you promote it. _rejected lists handles the '
                            'filter dropped, so they are not re-checked; delete an entry to '
                            'give it another look.')
    existing['last_run'] = datetime.now().isoformat(timespec='seconds')
    with open(CANDIDATES, 'w') as f:
        json.dump(existing, f, indent=2)
    print(f'[prospect] {len(new)} new candidates, {len(rejected)} rejected -> {CANDIDATES}')
    return new


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--seeds', type=int, default=6,
                    help='how many target-list accounts (tiers 1-3) to walk')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--no-following', action='store_true',
                    help='skip the seeds\' following lists, harvest repliers only')
    args = ap.parse_args()
    try:
        out = harvest(args.seeds, args.dry_run, with_following=not args.no_following)
        if not out:
            print('[SILENT] no new candidates')
    except Exception as e:
        print(f'[prospect] failed: {e}', file=sys.stderr)
        sys.exit(1)
