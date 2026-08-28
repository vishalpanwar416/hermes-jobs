"""Separate real job posts from engagement bait and scams.

The feed monitors used to accept anything matching a hiring word AND a tech
word. That let through a lot of noise, and one of its "hiring" signals was
`dm me`, which is actually one of the strongest bait markers.

This module scores a post instead. The single most useful question is not what
the text says, it is whether there is a verifiable way to apply: a real job
links to an applicant tracking system or a company careers page. Bait asks you
to DM, comment, or follow, because that is the entire point of the post.

Verdicts:
    genuine  apply to this
    review   plausible but unverified, worth a human glance
    reject   bait, scam, or unusable

Usage:
    from job_filter import classify
    v = classify(text, author='someuser', links=[...], author_post_count=3)
    v.verdict, v.score, v.reasons

Backtest against everything already captured:
    python job_filter.py --backtest
"""

import re
import os
import hashlib
import sqlite3
from dataclasses import dataclass, field
from typing import List

JOBS_DB = os.path.expanduser('~/.hermes/data/jobs_tracker.db')

# A real application path. Recruiters use these; bait accounts do not.
ATS_DOMAINS = [
    'greenhouse.io', 'lever.co', 'ashbyhq.com', 'myworkdayjobs.com', 'workday',
    'smartrecruiters.com', 'workable.com', 'breezy.hr', 'jobvite.com',
    'wellfound.com', 'angel.co', 'ycombinator.com', 'teamtailor.com',
    'recruitee.com', 'personio.', 'bamboohr.com', 'icims.com', 'taleo.net',
    'successfactors', 'oraclecloud.com', 'dover.com', 'rippling.com',
    'linkedin.com/jobs', 'indeed.com', 'naukri.com', 'instahyre.com',
    'cutshort.io', 'hirist.com',
]

CAREERS_PATH = re.compile(r'(careers?\.[a-z0-9-]+\.|/careers|/jobs?/|/join-us|/opportunities)', re.I)

# Contact channels that legitimate companies essentially never use as the sole
# application route for an engineering role.
SCAM_LINK_DOMAINS = ['t.me', 'telegram.', 'wa.me', 'whatsapp.com', 'chat.whatsapp']

# Creator monetisation funnels. A post linking here is selling a call, a course
# or a resume review; it is content marketing wearing a job post's clothes.
SELF_PROMO_DOMAINS = [
    'topmate.io', 'superprofile.bio', 'gumroad.com', 'razorpay.me', 'buymeacoffee',
    'ko-fi.com', 'stan.store', 'beacons.ai', 'linktr.ee', 'patreon.com',
]

# Hard rejects. Any one of these means the post is not a real job.
HARD_REJECT = [
    (re.compile(r'\b(comment|drop)\s*["\']?(yes|interested|info|me)\b', re.I),
     'comment-to-apply bait'),
    (re.compile(r'\blike\s*(and|&|\+)\s*(repost|retweet|rt)\b', re.I), 'like+repost bait'),
    (re.compile(r'\b(repost|retweet|rt)\s+(this|to|for|and)\b', re.I), 'repost bait'),
    (re.compile(r'\bfollow\s+(me|us)\s+(and|to|first|then)\b', re.I), 'follow-first bait'),
    (re.compile(r'\bmust be following\b', re.I), 'follow-first bait'),
    (re.compile(r'\b(no experience (needed|required)|anyone can apply|beginners? welcome)\b', re.I),
     'no-experience-needed'),
    (re.compile(r'\bearn\s*[\$₹]?\s*[0-9,]+\s*(a|per|/)\s*(day|week)\b', re.I), 'earn-per-day scam'),
    (re.compile(r'\b(registration|processing|security)\s+fee\b', re.I), 'asks for a fee'),
    (re.compile(r'\b(web3|crypto|nft|defi|airdrop)\b.{0,40}\b(hiring|role|job)\b', re.I),
     'crypto/web3 recruiting'),
    (re.compile(r'\b(hiring|role|job)\b.{0,40}\b(web3|crypto|nft|defi|airdrop)\b', re.I),
     'crypto/web3 recruiting'),
]

DM_ONLY = re.compile(
    r'\b(dm\s*(me|us|for|with|if|to)|please dm|drop a dm|send.{0,10}\bdm\b|dms open|'
    r'my dms|slide into)\b', re.I)

SALARY = re.compile(
    r'([\$₹€£]|inr|usd|lpa)\s?[0-9]{1,3}[.,]?[0-9]*\s?(k|l|lpa|lakh|cr)?\s*'
    r'(-|to|–|—)\s*([\$₹€£]|inr|usd)?\s?[0-9]{1,3}', re.I)

LOCATION = re.compile(
    r'\b(remote|hybrid|onsite|on-site|wfh|work from home|bangalore|bengaluru|'
    r'hyderabad|pune|mumbai|delhi|noida|gurgaon|chennai|san francisco|new york|'
    r'london|berlin|singapore|dubai|relocat)\b', re.I)

ROLE = re.compile(
    r'\b(backend|front[- ]?end|full[- ]?stack|software|platform|infra(structure)?|'
    r'devops|sre|data|ml|machine learning|ai|founding)\s+(engineer|developer|dev)\b|'
    r'\b(sde|swe)\s*-?\s*[123ivx]*\b', re.I)

SENIORITY = re.compile(r'\b(intern|junior|jr|mid|senior|sr|staff|principal|lead|founding)\b', re.I)

# "Wikimedia Foundation is hiring", "Mercury is hiring", "HPE is hiring".
# Naming the employer is something real listings and honest aggregators do and
# bait almost never does, because bait has no employer to name. Without this,
# aggregators posting genuine roles scored the same as anonymous "DM me" posts.
COMPANY_HIRING = re.compile(
    r'\b([A-Z][A-Za-z0-9&.\-]{1,20}(?:\s+[A-Z][A-Za-z0-9&.\-]{1,20}){0,3})\s+'
    r'(is|are)\s+(currently\s+)?(hiring|looking for|recruiting)\b')

# "Hiring | Software Engineer | Flexiple" style headers.
PIPE_HEADER = re.compile(r'\bhiring\b\s*\|\s*[^|\n]{3,40}\s*\|\s*[A-Z]')

# Generic subjects that are not an employer name.
_NOT_A_COMPANY = re.compile(
    r'^(we|i|they|it|company|someone|somebody|client|startup|team|who|this|that|'
    r'my|our|the team|a client|hiring|urgently|currently|no one|nobody)$', re.I)

# Handles whose purpose clearly is not recruiting.
OFF_TOPIC_HANDLE = re.compile(r'(meme|funny|viral|quotes?|motivat|crypto|betting|adult|nsfw)', re.I)

# Accounts that post hiring content constantly. Aggregators are useful, but a
# high-frequency poster is also the shape of an engagement farmer, so it lowers
# confidence rather than rejecting outright.
HIGH_FREQUENCY_THRESHOLD = 5


@dataclass
class Verdict:
    verdict: str
    score: int
    reasons: List[str] = field(default_factory=list)
    has_apply_link: bool = False

    def __bool__(self):
        return self.verdict == 'genuine'


def _links_blob(links):
    return ' '.join(links or []).lower()


BAIT_DB = os.path.expanduser('~/.hermes/data/x_growth.db')


def _bait_conn():
    conn = sqlite3.connect(BAIT_DB, timeout=10)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS bait_accounts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            platform    TEXT NOT NULL,
            author      TEXT NOT NULL,
            strikes     INTEGER DEFAULT 0,
            last_reason TEXT,
            last_url    TEXT,
            last_seen   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            muted_at    TIMESTAMP,
            blocked_at  TIMESTAMP,
            allowlisted INTEGER DEFAULT 0,
            UNIQUE(platform, author)
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS bait_strikes (
            platform  TEXT NOT NULL,
            author    TEXT NOT NULL,
            post_key  TEXT NOT NULL,
            seen_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(platform, author, post_key)
        )
    ''')
    conn.commit()
    return conn


def _strike_key(url, text):
    """Stable identity for one post.

    Rejected posts are never written to the seen-table (they are skipped before
    it), so the same post is re-examined on every scan. Without a per-post key,
    one bait post scanned every 3 hours manufactures a strike each time and
    crosses the mute threshold within a day on the evidence of a single post.
    """
    if url:
        m = re.search(r'(\d{15,25})', url)
        if m:
            return m.group(1)
        return url.split('?')[0][:180]
    return hashlib.sha1((text or '')[:300].encode('utf-8', 'replace')).hexdigest()[:20]


def record_rejection(author, platform, verdict, url='', text=''):
    """Log an account whose post was rejected. One strike per DISTINCT post."""
    # Placeholder authors are not accounts. 'Connection' is the fallback the
    # LinkedIn monitor used when its author selector failed, and it was
    # accumulating strikes as if it were a real person.
    if not author or verdict.verdict != 'reject':
        return
    if author.strip().lower() in {'connection', 'unknown', '?', 'linkedin member'}:
        return
    reason = '; '.join(verdict.reasons[:2])
    key = _strike_key(url, text)
    conn = _bait_conn()
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO bait_strikes (platform, author, post_key) "
            "VALUES (?, ?, ?)", (platform, author, key))
        if not cur.rowcount:
            conn.commit()
            return  # already counted this exact post
        conn.execute(
            '''INSERT INTO bait_accounts (platform, author, strikes, last_reason, last_url)
               VALUES (?, ?, 0, ?, ?)
               ON CONFLICT(platform, author) DO UPDATE SET
                   last_reason = excluded.last_reason,
                   last_url    = excluded.last_url,
                   last_seen   = CURRENT_TIMESTAMP''',
            (platform, author, reason, url))
        # Strikes are always derived from distinct posts, never incremented.
        conn.execute(
            '''UPDATE bait_accounts SET strikes =
                   (SELECT COUNT(*) FROM bait_strikes b
                    WHERE b.platform = bait_accounts.platform
                      AND b.author = bait_accounts.author)
               WHERE platform = ? AND author = ?''', (platform, author))
        conn.commit()
    finally:
        conn.close()


def author_post_count(author, db_path=JOBS_DB):
    """How many hiring posts we have already logged from this account."""
    if not author:
        return 0
    try:
        conn = sqlite3.connect(db_path)
        n = conn.execute(
            "SELECT COUNT(*) FROM job_openings WHERE job_url LIKE ?",
            (f'%x.com/{author}/status%',)).fetchone()[0]
        conn.close()
        return n
    except sqlite3.Error:
        return 0


def classify(text, author='', links=None, verified=False, post_count=None):
    """Score one post. Returns a Verdict."""
    text = text or ''
    links = links or []
    blob = _links_blob(links)
    reasons = []
    score = 0

    for pattern, label in HARD_REJECT:
        if pattern.search(text):
            return Verdict('reject', -99, [label])

    if any(d in blob for d in SCAM_LINK_DOMAINS):
        return Verdict('reject', -99, ['telegram/whatsapp as the application channel'])

    # --- the decisive signal: is there a real way to apply? ---
    if any(d in blob for d in SELF_PROMO_DOMAINS):
        return Verdict('reject', -99, ['links to a paid coaching/creator funnel'])

    has_ats = any(d in blob for d in ATS_DOMAINS)
    has_careers = bool(CAREERS_PATH.search(blob))
    has_any_link = bool(links)

    if has_ats:
        score += 4
        reasons.append('links to an applicant tracking system')
    elif has_careers:
        score += 3
        reasons.append('links to a careers page')
    elif has_any_link:
        score += 1
        reasons.append('has an external link')

    dm_only = bool(DM_ONLY.search(text))
    if dm_only and not (has_ats or has_careers):
        score -= 3
        reasons.append('DM is the only way to apply')

    if not has_any_link and not dm_only:
        score -= 1
        reasons.append('no application link')

    # --- specificity: real posts describe the job ---
    if SALARY.search(text):
        score += 1
        reasons.append('states compensation')
    if LOCATION.search(text):
        score += 1
        reasons.append('states location or remote policy')
    m = COMPANY_HIRING.search(text)
    if m and not _NOT_A_COMPANY.match(m.group(1).strip()):
        score += 2
        reasons.append(f'names the employer ({m.group(1).strip()[:28]})')
    elif PIPE_HEADER.search(text):
        score += 2
        reasons.append('names the employer')

    has_role = bool(ROLE.search(text))
    if has_role:
        score += 1
        reasons.append('names a concrete role')
    # Seniority only counts alongside a real role title. On its own it fires on
    # ordinary prose ("if AI makes junior developers more productive...") and was
    # pushing commentary threads into the review bucket.
    if has_role and SENIORITY.search(text):
        score += 1
        reasons.append('states seniority')

    # Commentary about the job market is not a job posting. These openers are
    # how an opinion thread starts, never how a vacancy is announced.
    if re.search(r"\b(i don'?t understand|unpopular opinion|hot take|am i the only|"
                 r"why do(es)? (companies|recruiters|everyone)|genuine question|"
                 r"thoughts\?|what do you think)\b", text, re.I):
        score -= 3
        reasons.append('reads as commentary, not a vacancy')

    body = re.sub(r'https?://\S+', '', text)
    if len(body.strip()) < 120:
        score -= 1
        reasons.append('very short post')

    # --- account signals ---
    if verified:
        score += 1
        reasons.append('verified account')

    if OFF_TOPIC_HANDLE.search(author or ''):
        score -= 2
        reasons.append(f'account @{author} is not a recruiting account')

    if post_count is None:
        post_count = author_post_count(author)
    if post_count >= HIGH_FREQUENCY_THRESHOLD:
        score -= 2
        reasons.append(f'@{author} has posted {post_count} hiring tweets already')

    if score >= 4:
        verdict = 'genuine'
    elif score >= 1:
        verdict = 'review'
    else:
        verdict = 'reject'

    return Verdict(verdict, score, reasons, has_apply_link=has_ats or has_careers)


# ---------------------------------------------------------------------------
# backtest
# ---------------------------------------------------------------------------

def backtest(limit=None):
    """Replay every captured X post through the filter.

    Link data was never stored for these rows, so link-based points cannot be
    awarded. Real-world results will score higher than this; treat the output as
    a floor and read it for which posts get REJECTED, which is the point.
    """
    conn = sqlite3.connect(JOBS_DB)
    rows = conn.execute(
        "SELECT job_url, job_description FROM job_openings "
        "WHERE source LIKE '%x%' ORDER BY id DESC").fetchall()
    conn.close()
    if limit:
        rows = rows[:limit]

    counts = {'genuine': 0, 'review': 0, 'reject': 0}
    buckets = {'genuine': [], 'review': [], 'reject': []}

    for url, text in rows:
        m = re.search(r'x\.com/([^/]+)/status', url or '')
        author = m.group(1) if m else ''
        urls = re.findall(r'https?://\S+', text or '')
        v = classify(text or '', author=author, links=urls)
        counts[v.verdict] += 1
        buckets[v.verdict].append((author, v, (text or '').replace('\n', ' ')))

    total = len(rows) or 1
    print(f"Backtest over {len(rows)} captured X posts\n")
    for k in ('genuine', 'review', 'reject'):
        print(f"  {k:8s} {counts[k]:4d}  ({100*counts[k]/total:.0f}%)")
    print()
    for k in ('reject', 'genuine'):
        print(f"--- {k.upper()} samples ---")
        for author, v, text in buckets[k][:6]:
            print(f"  @{author} (score {v.score}): {text[:88]}")
            print(f"     {'; '.join(v.reasons[:3])}")
        print()
    return counts


if __name__ == '__main__':
    import sys
    if '--backtest' in sys.argv:
        backtest()
    else:
        print(__doc__)
