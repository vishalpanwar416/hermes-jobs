#!/usr/bin/env python3
"""Scan X home feed for high-signal tech posts and add valuable comments.

Quality over quantity — up to 2 replies per run. Uses social_voice.draft_comment()
to generate unique, on-topic replies.

Usage:
    python x_feed_engage.py            # live, posts replies
    python x_feed_engage.py --dry-run  # scan + draft, post nothing
"""
import os
import re
import sys
import json
import time
import random
import sqlite3
import hashlib
import argparse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import pipeline_log as _plog
    _plog.start('x_feed_engage')
except Exception:
    pass

import social_voice as voice

DB_PATH = os.path.expanduser('~/.hermes/data/x_growth.db')
PROFILE = os.path.expanduser('~/.hermes/data/chrome_automation_profile')

MAX_REPLIES = 2

# Signal keywords grouped by topic — posts are worth engaging with if they
# match enough of these.
SIGNAL_KEYWORDS = {
    'backend_general': [
        'backend', 'api', 'microservice', 'server', 'latency', 'throughput',
        'p95', 'p99', 'scal', 'perform', 'optimize', 'p99', 'benchmark',
    ],
    'go': [
        'golang', 'go ', ' goroutine', 'sync.pool', 'pprof', 'gomemlimit',
        'concurren', 'channel',
    ],
    'infra': [
        'kubernetes', 'k8s', 'terraform', 'docker', 'deploy', 'pipeline',
        'ci/cd', 'monitoring', 'observability', 'prometheus', 'grafana',
        'cloud', 'gcp', 'aws', 'infra', 'infrastructure',
    ],
    'databases': [
        'postgres', 'postgresql', 'redis', 'kafka', 'database', 'query',
        'cache', 'caching', 'sharding', 'index', 'migration', 'stale-while-revalidate',
    ],
    'ai_agents': [
        'ai ', 'llm', 'rag', 'agent', 'mcp', 'model context protocol',
        'inference', 'embedding', 'vector', 'prompt', 'chain', 'tool use',
        'function calling', 'structured output',
    ],
    'system_design': [
        'system design', 'distributed', 'architecture', 'failover',
        'circuit breaker', 'backpressure', 'race', 'deadlock', 'thundering',
        'pool exhaust', 'connection pool',
    ],
    'production': [
        'prod', 'incident', 'outage', 'on-call', 'debug', 'root cause',
        'postmortem', 'crash', 'memory leak', 'gc ', 'allocation',
    ],
    'eng_culture': [
        'engineering', 'developer', 'programming', 'ship', 'shipping',
        'build', 'builder', 'startup', 'founder', 'cto', 'staff engineer',
    ],
}

# Post content mentioning these is NOT worth engaging with (engagement farming,
# crypto spam, political rants, generic motivational).
NOGO_SIGNALS = [
    'follow for follow', 'f4f', 'like and follow', 'retweet to win',
    'dm for promo', 'link in bio', 'giveaway', 'free money', 'crypto giveaway',
    'nft', 'mint', 'buy my', 'sign up with my code', 'referral link',
    'subscribe to my', '#motivation', '#inspiration', 'hustle culture',
    'grindset', 'boss mindset', 'cash app', 'send me',
]


def init_tables():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('''
    CREATE TABLE IF NOT EXISTS feed_comments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        post_url TEXT UNIQUE NOT NULL,
        author TEXT,
        post_text TEXT,
        comment_text TEXT,
        status TEXT DEFAULT 'posted',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    conn.commit()
    conn.close()


def already_commented(post_url):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM feed_comments WHERE post_url = ?", (post_url,))
    row = cur.fetchone()
    conn.close()
    return row is not None


def record_comment(post_url, author, post_text, comment_text, status='posted'):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('''
    INSERT OR IGNORE INTO feed_comments (post_url, author, post_text, comment_text, status)
    VALUES (?, ?, ?, ?, ?)
    ''', (post_url, author, post_text[:500], comment_text, status))
    conn.commit()
    conn.close()


def score_post(text):
    """Score a post based on signal keywords. Higher = more worth replying to."""
    t = text.lower()
    score = 0
    matched_categories = set()

    for category, keywords in SIGNAL_KEYWORDS.items():
        for kw in keywords:
            if kw in t:
                matched_categories.add(category)
                score += 1
                break

    # Bonus for depth — posts with specific technical terms are better
    depth_terms = ['p95', 'p99', 'sync.pool', 'circuit breaker', 'stale-while-revalidate',
                   'connection pool', 'pool exhaust', 'thundering herd', 'backpressure',
                   'goroutine', 'channel', 'pprof', 'gomemlimit', 'raft', 'consensus']
    for dt in depth_terms:
        if dt in t:
            score += 2

    # Penalise no-go content
    for ng in NOGO_SIGNALS:
        if ng in t:
            return -1

    # Weight by category match breadth — posts spanning multiple topics are richer
    return score + len(matched_categories)


def is_bait_post(text):
    """Quick filter for obvious engagement bait or low-signal content."""
    t = text.lower()
    bait_patterns = [
        'like if you', 'retweet if', 'follow for more',
        'what is your', 'type your', 'comment down below',
        'tag someone who', 'share this,', 'this is why',
        'thread 🧵', 'thread \U0001f9f5',
    ]
    for b in bait_patterns:
        if b in t:
            return True
    return False


def collect_feed_posts(page):
    """Scan X home feed for high-signal tech posts."""
    page.goto('https://x.com/home', timeout=45000, wait_until='domcontentloaded')
    time.sleep(4)

    # Check login
    if any(x in page.url.lower() for x in ('login', 'i/flow', 'authorize')):
        print('[feed] not logged in — aborting', file=sys.stderr)
        return []

    # Scroll a few times to load enough posts
    for _ in range(3):
        page.evaluate('window.scrollBy(0, 1200)')
        time.sleep(2)

    articles = page.locator('article[data-testid="tweet"]').all()
    print(f'[feed] {len(articles)} posts visible', file=sys.stderr)

    candidates = []
    for art in articles:
        try:
            text = art.inner_text()
            # Skip short/promotional posts
            if len(text.strip()) < 40:
                continue
            if is_bait_post(text):
                continue

            # Extract post URL
            post_url = ''
            author = ''
            for link in art.locator('a').all():
                try:
                    href = link.get_attribute('href') or ''
                    if '/status/' in href:
                        post_url = ('https://x.com' + href) if href.startswith('/') else href
                        post_url = post_url.split('?')[0].split('/analytics')[0]
                    elif not author and href.startswith('/') and '/home' not in href and '/status/' not in href:
                        author = href.strip('/')
                except Exception:
                    continue

            if not post_url or not author:
                continue
            if already_commented(post_url):
                continue

            score = score_post(text)
            if score > 0:
                candidates.append({
                    'url': post_url,
                    'author': author,
                    'text': text[:1500],
                    'score': score,
                })
                print(f'  [{score}] @{author}: {text[:80]}...', file=sys.stderr)

        except Exception as e:
            print(f'[feed] skipped an item: {e}', file=sys.stderr)
            continue

    # Sort by score, pick best
    candidates.sort(key=lambda c: c['score'], reverse=True)
    print(f'[feed] {len(candidates)} high-signal candidates', file=sys.stderr)
    return candidates[:MAX_REPLIES]


def main(dry_run=False):
    init_tables()

    from playwright.sync_api import sync_playwright
    from browser_lock import profile_lock, BrowserBusy

    try:
        with profile_lock('x_feed_engage'):
            with sync_playwright() as p:
                ctx = p.chromium.launch_persistent_context(
                    user_data_dir=PROFILE, channel='chrome', headless=True,
                    args=['--disable-blink-features=AutomationControlled', '--no-sandbox'])
                page = ctx.new_page()
                page.set_viewport_size({'width': 1440, 'height': 1000})

                posts = collect_feed_posts(page)
                if not posts:
                    print('[feed] no worthwhile posts found', file=sys.stderr)
                    ctx.close()
                    print('[SILENT] nothing worth replying to')
                    return

                commented = 0
                for post in posts:
                    if commented >= MAX_REPLIES:
                        break

                    body = voice.strip_x_chrome(post['text'])
                    comment = voice.draft_comment(post['author'], body, platform='x')
                    if comment is voice.SKIP or not comment:
                        print(f'[feed] model declined @{post["author"]}', file=sys.stderr)
                        continue
                    # 2026-09-10: this script pitched multi-qr under a "looking
                    # to connect with managers" post and "1k RPS on a Go API"
                    # under an AI-engineering hot take. Both drafts were fine as
                    # text and irrelevant as replies. Score the pair before
                    # posting; an unscorable reply is not posted.
                    score = voice.judge_relevance(body, comment)
                    if score is None or score < voice.RELEVANCE_MIN:
                        print(f'[feed][judge] {score}/5 for @{post["author"]}, dropped: '
                              f'{comment[:100]!r}', file=sys.stderr)
                        continue
                    print(f'[feed][judge] {score}/5 for @{post["author"]}', file=sys.stderr)

                    if dry_run:
                        print(f'[feed][dry-run] would reply to @{post["author"]} on {post["url"]}')
                        print(f'  reply: {comment}')
                        record_comment(post['url'], post['author'], post['text'],
                                       comment, status='dry-run')
                        commented += 1
                        continue

                    try:
                        page.goto(post['url'], timeout=40000, wait_until='domcontentloaded')
                        time.sleep(3)

                        # Click reply button
                        reply_btn = page.locator('[data-testid="reply"]').first
                        if not reply_btn.count() or not reply_btn.is_visible():
                            print(f'[feed] no reply button on {post["url"]}', file=sys.stderr)
                            continue

                        reply_btn.click()
                        time.sleep(2)

                        # Type the reply
                        page.keyboard.type(comment, delay=8)
                        time.sleep(1)

                        # Submit with Ctrl+Enter
                        page.keyboard.press('Control+Enter')
                        time.sleep(5)

                        record_comment(post['url'], post['author'], post['text'], comment)
                        print(f'[feed] replied to @{post["author"]}: {comment[:100]}...')
                        commented += 1
                        time.sleep(random.uniform(3, 6))

                    except Exception as e:
                        print(f'[feed] failed to reply on {post["url"]}: {e}', file=sys.stderr)
                        record_comment(post['url'], post['author'], post['text'],
                                       str(e)[:200], status='failed')
                        continue

                ctx.close()

    except BrowserBusy:
        print('[feed] chrome profile busy — will retry next run', file=sys.stderr)
        print('[SILENT] browser busy')
        return

    if commented == 0:
        print('[SILENT] no replies posted this run')
    else:
        print(f'[feed] {commented} replies posted this run')


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()
    main(dry_run=args.dry_run)