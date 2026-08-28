"""One dedupe ledger shared by every script that replies on Vishal's behalf.

Two jobs answer inbound mentions on X and LinkedIn:

    auto_respond_social_replies.py   social_all_reply_inbound          (30m)
    auto_reply_inbound.py            social_inbound_reply_x_linkedin   (120m)

They kept separate tables (social_replies_handled, inbound_replies,
linkedin_inbound_replies), so neither could see the other's work and both
replied to the same person. That already happened once: four near-identical
replies went to @officialdev_bml.

Per-post ids cannot fix this, because the two scripts reach the same interaction
by different routes and mint different ids for it: one reads a post's comment
list, the other reads the notifications feed. So the key here is derived from
WHAT WAS SAID, not from where it was found.

    from shared_dedupe import already_answered, mark_answered

    if already_answered('x', author, their_text):
        continue
    ...
    mark_answered('x', author, their_text, reply, source='auto_reply_inbound')
"""

import os
import re
import sqlite3
import hashlib

DB_PATH = os.path.expanduser('~/.hermes/data/x_growth.db')


def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS answered_interactions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            platform      TEXT NOT NULL,
            author        TEXT,
            content_key   TEXT NOT NULL,
            incoming_text TEXT,
            reply_text    TEXT,
            source        TEXT,
            created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(platform, content_key)
        )
    ''')
    conn.commit()
    return conn


_CHROME = {
    'feed', 'post', 'suggested', 'promoted', 'follow', 'following', 'replying',
    'like', 'reply', 'repost', 'send', 'share', 'comment', 'comments', 'more',
    'ago', 'edited', 'author', 'premium', 'connection', 'view', 'profile',
}
_STOP = {
    'this', 'that', 'with', 'from', 'they', 'them', 'then', 'than', 'have',
    'has', 'had', 'was', 'were', 'been', 'being', 'and', 'but', 'for', 'not',
    'you', 'your', 'yours', 'the', 'are', 'its', 'it', 'is', 'of', 'to', 'in',
    'on', 'at', 'as', 'be', 'by', 'or', 'if', 'so', 'we', 'our', 'us', 'a', 'an',
}
_SIGNATURE_WORDS = 14


def content_key(author, text):
    """Stable identity for an interaction, from what was actually said.

    Hashing the normalised text failed: the same comment reaches the two
    scrapers with different surrounding chrome ("Feed post", "16h", "Replying
    to", "Like Reply"), which shifts the truncation window and produces two
    different keys for one interaction.

    So build an order-independent signature instead: drop chrome and stopwords,
    keep the distinctive words, sort them, hash a fixed number. Both views of
    the same comment share those words regardless of what surrounds them.

    A collision means declining to reply, which is the safe direction. A miss
    means replying twice, which is what this exists to prevent.
    """
    t = (text or '').lower()
    t = re.sub(r'https?://\S+', ' ', t)
    t = re.sub(r'@\w+', ' ', t)
    t = re.sub(r'[^a-z0-9 ]+', ' ', t)

    author_tokens = {w for w in re.split(r'\W+', (author or '').lower()) if w}
    words = []
    for w in t.split():
        if len(w) < 4 or w in _CHROME or w in _STOP or w in author_tokens:
            continue
        if w.isdigit():
            continue
        if w not in words:
            words.append(w)

    signature = sorted(words)[:_SIGNATURE_WORDS]
    if not signature:
        # Nothing distinctive left; fall back to the raw text so short replies
        # still dedupe against themselves.
        signature = [' '.join((text or '').lower().split())[:120]]
    raw = '|'.join(signature)
    return hashlib.sha1(raw.encode('utf-8', 'replace')).hexdigest()[:24]


def already_answered(platform, author, text):
    if not text:
        return False
    key = content_key(author, text)
    conn = _conn()
    try:
        row = conn.execute(
            'SELECT 1 FROM answered_interactions WHERE platform = ? AND content_key = ?',
            (platform, key)).fetchone()
        return row is not None
    finally:
        conn.close()


def mark_answered(platform, author, text, reply, source=''):
    """Record an answered interaction. Returns False if it was already there."""
    if not text:
        return False
    key = content_key(author, text)
    conn = _conn()
    try:
        cur = conn.execute(
            '''INSERT OR IGNORE INTO answered_interactions
                 (platform, author, content_key, incoming_text, reply_text, source)
               VALUES (?, ?, ?, ?, ?, ?)''',
            (platform, author, key, (text or '')[:400], (reply or '')[:400], source))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def backfill():
    """Seed the ledger from the three legacy tables.

    Without this the first run after switching would treat every historical
    reply as unanswered and answer it all over again.
    """
    conn = _conn()
    added = 0
    legacy = [
        ("SELECT 'x', author, tweet_text, reply_text FROM inbound_replies", 'inbound_replies'),
        ("SELECT platform, author, incoming_text, reply_generated FROM social_replies_handled",
         'social_replies_handled'),
        ("SELECT 'linkedin', author, notif_text, reply_text FROM linkedin_inbound_replies",
         'linkedin_inbound_replies'),
    ]
    for sql, name in legacy:
        try:
            rows = conn.execute(sql).fetchall()
        except sqlite3.OperationalError:
            continue
        for platform, author, text, reply in rows:
            if not text:
                continue
            cur = conn.execute(
                '''INSERT OR IGNORE INTO answered_interactions
                     (platform, author, content_key, incoming_text, reply_text, source)
                   VALUES (?, ?, ?, ?, ?, ?)''',
                (platform or 'x', author, content_key(author, text),
                 text[:400], (reply or '')[:400], f'backfill:{name}'))
            added += cur.rowcount
    conn.commit()
    conn.close()
    return added


if __name__ == '__main__':
    import json
    n = backfill()
    conn = _conn()
    total = conn.execute('SELECT COUNT(*) FROM answered_interactions').fetchone()[0]
    by = conn.execute(
        'SELECT platform, COUNT(*) FROM answered_interactions GROUP BY platform').fetchall()
    conn.close()
    print(json.dumps({'backfilled': n, 'total': total, 'by_platform': dict(by)}, indent=2))
