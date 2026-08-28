import os
import sys
import time
import json
import sqlite3
import re
import random
import urllib.request
from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# Capture this run's full stdout and stderr to
# ~/.hermes/logs/pipelines/social_x_reply_outbound/ so a failed or silent run can be
# diagnosed afterwards instead of vanishing.
if __name__ == '__main__':
    # Only when run directly. Firing on import made every script that
    # imports this module log its own run under this pipeline's name.
    try:
        import pipeline_log as _plog
        _plog.start('social_x_reply_outbound')
    except Exception:
        pass

import social_voice as voice
from browser_lock import profile_lock, BrowserBusy

DB_PATH = os.path.expanduser('~/.hermes/data/x_growth.db')
AUTOMATION_PROFILE_DIR = os.path.expanduser('~/.hermes/data/chrome_automation_profile')

# Targeted technical search queries across high-engagement X discussion spaces
SEARCH_QUERIES = [
    # Go / Backend
    '(#golang OR "golang" OR "golang developer") ("concurrency" OR "p95" OR "latency" OR "backend" OR "goroutines") -filter:links min_faves:5',
    '("golang" OR "go") ("sync.pool" OR "pprof" OR "gin" OR "microservices") min_faves:5',
    # AI Agents / MCP / RAG
    '("ai agents" OR "mcp" OR "fastmcp" OR "llm in prod") ("tools" OR "evaluation" OR "guardrails" OR "rag" OR "agents") min_faves:5',
    '("model context protocol" OR "mcp server" OR "agentic") ("architecture" OR "production" OR "eval") min_faves:5',
    # DB Scaling / Redis / Postgres
    '("redis" OR "postgresql" OR "postgres") ("caching" OR "scaling" OR "query optimization" OR "thundering herd") min_faves:5',
    # Kubernetes / SRE
    '("kubernetes" OR "k8s") ("disaster recovery" OR "keda" OR "failover" OR "sre" OR "observability") min_faves:5',
    # Tech Showcase / Build in Public
    '("what are you building" OR "drop your project" OR "showcase your build" OR "share your saas") ("developer" OR "code" OR "github" OR "ai") min_faves:10',
    '("show your project" OR "drop your startup" OR "showcase your startup") ("tech" OR "developer" OR "engineer" OR "ai") min_faves:10'
]

BANNED_KEYWORDS = [
    'game', 'gaming', 'pokemon', 'cosplay', 'nintendo', 'switch', 'ps5', 'xbox', 'elden ring',
    'anime', 'manga', 'movie', 'trailer', 'crypto giveaway', 'airdrop', 'casino', 'betting',
    'hiring alert', 'trainee software', 'job opening', 'recruitment', 'fresher', 'onlyfans'
]

def init_replies_table():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('''
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

def is_tweet_replied(tweet_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id FROM auto_replies WHERE tweet_id = ?", (tweet_id,))
    row = cur.fetchone()
    conn.close()
    return row is not None

def has_used_recent_reply(reply_text, limit=6):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT reply_text FROM auto_replies ORDER BY id DESC LIMIT ?", (limit,))
    rows = cur.fetchall()
    conn.close()
    return any(row[0] == reply_text for row in rows)

def is_author_recently_replied(author, limit=10):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT author FROM auto_replies ORDER BY id DESC LIMIT ?", (limit,))
    rows = cur.fetchall()
    conn.close()
    return any(row[0].lower() == author.lower() for row in rows)

def record_reply(tweet_id, author, tweet_text, reply_text):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('''
    INSERT OR IGNORE INTO auto_replies (tweet_id, author, tweet_text, reply_text)
    VALUES (?, ?, ?, ?)
    ''', (tweet_id, author, tweet_text, reply_text))
    conn.commit()
    conn.close()

REPLY_POOLS = {
    "showcase": [
        "Building Table-Tap (live QR ordering + GPT-4 RAG on GCP Cloud Run) and recently open-sourced `multi-qr` (npm i multi-qr) to pack WiFi + URLs into single QR payloads. Full work: https://vishalpanwar.in",
        "Shipped `multi-qr` recently on npm — a lightweight codec to pack WiFi, URLs, and contacts into a single scannable QR without exceeding byte limits: https://github.com/vishalpanwar416/multi-qr",
        "Working on Table-Tap (live contactless ordering + RAG analytics on Cloud Run) and agent backends with MCP tool-calling. My work: https://vishalpanwar.in"
    ],
    "golang": [
        "A big win for us in Go at 1K+ RPS was using sync.Pool to reuse memory buffers in hot JSON paths. Slashed GC pauses and cut p95 latency by ~40%.",
        "Streaming JSON encoders and bounded worker pools make a massive difference in Go APIs under traffic spikes compared to spawning unbounded goroutines.",
        "Profiling Go with pprof under load showed us that memory allocation churn in middleware was eating 30% of CPU time before we switched to buffer pooling."
    ],
    "ai_agents": [
        "The biggest barrier to reliable agents in prod is nondeterministic tool-calling. Adding strict schema validation and evaluation guardrails before any outbound mutation is essential.",
        "Most LLM wrappers break when chained across tools. Using MCP for structured tool discovery and adding automated eval loops makes multi-step execution deterministic.",
        "We found that chunking by semantic boundaries combined with BM25 hybrid re-ranking pushed RAG query accuracy from 70% to 90%+ in production."
    ],
    "databases": [
        "Pairing Redis with stale-while-revalidate and distributed locks on cache misses saved our Postgres DB from thundering herds during traffic spikes. Dropped peak load 60%.",
        "Tiered TTLs based on entity volatility and caching query aggregates cut repeat DB load by over 50% on our high-traffic endpoints.",
        "Circuit breakers on external data vendors combined with Bull/Redis queues prevent flaky third-party APIs from cascading into database connection pool exhaustion."
    ],
    "infra_devops": [
        "Running live disaster recovery restore drills in K8s/GCP always surfaces unexpected service dependency order bugs. Automated failover runbooks are non-negotiable.",
        "Automating our CI/CD pipelines via Jenkins and Terraform dropped deployment overhead by 85% while keeping production uptime at 99.9%."
    ]
}

# Pure follow farming. Showcase threads ("drop your project") are deliberately
# NOT here: replying to a fresh one with what he actually built is a reasonable
# way to get seen. These are the ones with no technical surface at all.
_ENGAGEMENT_FARM = [
    'follow me and i', 'follow for follow', 'f4f', 'like and retweet',
    'retweet to enter', 'tag 3 friends', 'comment "yes"', "comment 'yes'",
    'drop a gm', 'gm fam', 'who wants free', 'giveaway', 'airdrop',
]

# Specific enough that the tweet is genuinely about engineering.
_STRONG_TECH = [
    'developer', 'engineer', 'backend', 'frontend', 'coding', 'software', 'api',
    'code', 'github', 'saas', 'golang', 'python', 'kubernetes', 'k8s', 'docker',
    'redis', 'postgres', 'database', 'mcp', 'rag', 'llm', 'architecture',
    'latency', 'throughput', 'cache', 'caching', 'goroutine', 'concurrency',
    'deploy', 'scaling', 'microservice', 'microservices', 'devops', 'terraform',
    'p95', 'p99', 'pprof', 'grpc', 'kafka', 'typescript', 'javascript', 'rust',
    'node', 'nextjs', 'fastapi', 'embedding', 'retrieval', 'inference',
]

# Too generic to qualify a tweet on their own. "Building" and "project" were in
# the accept list before, which is why networking threads kept getting through.
_WEAK_TECH = ['building', 'build in public', 'project', 'startup', 'agent', 'ai', 'go']


def _mentions(text, terms):
    """Word boundary match.

    Substring matching meant 'go' fired on "going" and "good", so almost any
    tweet counted as Go content.
    """
    return any(re.search(rf'(?<![a-z0-9]){re.escape(term)}s?(?![a-z0-9])', text)
               for term in terms)


def is_valid_tech_context(text):
    t = text.lower()
    if any(b in t for b in BANNED_KEYWORDS):
        return False
    if any(b in t for b in _ENGAGEMENT_FARM):
        return False

    if _mentions(t, _STRONG_TECH):
        return True

    # A weak term alone is not enough, but a weak term plus a showcase prompt is
    # a build-in-public thread worth answering.
    showcase = any(k in t for k in [
        'drop your', 'show me what you', 'share your project', 'what are you building',
        'what did you ship', 'showcase', 'what have you built',
    ])
    return showcase and _mentions(t, _WEAK_TECH)

def generate_technical_reply(tweet_text, author='someone'):
    """Draft a reply to a stranger's tweet.

    The REPLY_POOLS below stay as a fallback, but they used to be the whole
    strategy: the same handful of sentences went out over and over, which reads
    as a bot to anyone who saw two of them. The model writes to the actual tweet
    now, and social_voice strips the machine tells afterwards.
    """
    if not is_valid_tech_context(tweet_text):
        return None

    drafted = voice.draft_comment(author, tweet_text, platform='x')
    if drafted is voice.SKIP:
        print(f"[voice] declined to reply to @{author}", file=sys.stderr)
        return None
    if drafted and not has_used_recent_reply(drafted):
        return drafted

    t = tweet_text.lower()

    # 1. Project showcase / Build in public (tech only)
    if any(k in t for k in ['drop your', 'show me what you', 'share your project', 'what are you building', 'built this week', 'what did you ship', 'showcase', 'drop your startup', 'show your project']):
        for opt in random.sample(REPLY_POOLS["showcase"], len(REPLY_POOLS["showcase"])):
            if not has_used_recent_reply(opt):
                return opt
                
    # 2. Go / Concurrency / Performance
    if any(k in t for k in ['golang', 'go lang', 'goroutine', 'sync.pool', 'gin', 'go api', 'p95', 'gc pause']):
        for opt in random.sample(REPLY_POOLS["golang"], len(REPLY_POOLS["golang"])):
            if not has_used_recent_reply(opt):
                return opt
                
    # 3. AI / MCP / Agents / RAG
    if any(k in t for k in ['mcp', 'agentic', 'ai agent', 'tool calling', 'fastmcp', 'langchain', 'rag', 'llm in prod', 'evals', 'model context protocol']):
        for opt in random.sample(REPLY_POOLS["ai_agents"], len(REPLY_POOLS["ai_agents"])):
            if not has_used_recent_reply(opt):
                return opt
                
    # 4. Redis / Postgres / Database scaling
    if any(k in t for k in ['redis', 'caching', 'postgres', 'database load', 'slow query', 'thundering herd', 'query optimization']):
        for opt in random.sample(REPLY_POOLS["databases"], len(REPLY_POOLS["databases"])):
            if not has_used_recent_reply(opt):
                return opt
                
    # 5. K8s / SRE / Disaster Recovery
    if any(k in t for k in ['kubernetes', 'k8s', 'failover', 'disaster recovery', 'keda', 'terraform', 'outage', 'sre']):
        for opt in random.sample(REPLY_POOLS["infra_devops"], len(REPLY_POOLS["infra_devops"])):
            if not has_used_recent_reply(opt):
                return opt

    return None

def scan_and_reply(max_replies=5, dry_run=False):
    init_replies_table()
    replies_done = []
    
    # Auto-clean lingering locks before launching
    for lock_name in ['SingletonLock', 'SingletonSocket', 'SingletonCookie']:
        p = os.path.join(AUTOMATION_PROFILE_DIR, lock_name)
        if os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass
                
    with sync_playwright() as p:
        try:
            context = p.chromium.launch_persistent_context(
                user_data_dir=AUTOMATION_PROFILE_DIR,
                channel="chrome",
                headless=True,
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--no-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-gpu'
                ]
            )
            page = context.new_page()
            page.set_viewport_size({"width": 1440, "height": 900})
            
            # Run across 2-3 targeted search queries to get maximum high-reach posts
            sampled_queries = random.sample(SEARCH_QUERIES, min(3, len(SEARCH_QUERIES)))
            
            for query in sampled_queries:
                if len(replies_done) >= max_replies:
                    break
                    
                # f=top with no time bound was surfacing tweets two months old.
                # A reply on a stale thread gets no impressions no matter how
                # good it is, so bound the search to the last few days and let
                # f=top rank within that window.
                dated_query = query if 'within_time:' in query else f"{query} within_time:3d"
                encoded_query = urllib.request.quote(dated_query)
                search_url = f"https://x.com/search?q={encoded_query}&f=top"
                
                print(f"Scanning high-reach query: {query}", file=sys.stderr)
                try:
                    page.goto(search_url, timeout=35000, wait_until="domcontentloaded")
                    time.sleep(4)
                except Exception:
                    continue
                
                for scroll in range(4):
                    if len(replies_done) >= max_replies:
                        break
                        
                    articles = page.locator('article[data-testid="tweet"]').all()
                    for art in articles:
                        if len(replies_done) >= max_replies:
                            break
                        try:
                            text = art.inner_text()
                            
                            links = art.locator('a').all()
                            tweet_url = ""
                            author = ""
                            for l in links:
                                href = l.get_attribute('href') or ''
                                if '/status/' in href and not tweet_url:
                                    tweet_url = href
                                if href.startswith('/') and not href.startswith('/home') and not '/status/' in href and not author:
                                    author = href.strip('/')
                                    
                            tweet_id = tweet_url.split('/status/')[-1].split('?')[0] if '/status/' in tweet_url else ""
                            
                            if not tweet_id or is_tweet_replied(tweet_id) or is_author_recently_replied(author) or author.lower() in ['vishalpanwarr', 'mynintendonews', 'jessayleee']:
                                continue
                                
                            reply_content = generate_technical_reply(text, author)
                            if reply_content:
                                print(f"\nFound high-reach discussion by @{author}:\n{text[:120]}...", file=sys.stderr)
                                print(f"Replying: {reply_content}", file=sys.stderr)

                                if dry_run:
                                    print(f"[dry-run] would reply to @{author}", file=sys.stderr)
                                    replies_done.append({
                                        "author": author,
                                        "tweet": text[:120],
                                        "reply": reply_content,
                                        "status": "dry-run",
                                    })
                                    continue

                                reply_btn = art.locator('[data-testid="reply"]').first
                                if reply_btn.is_visible():
                                    reply_btn.click()
                                    time.sleep(2)
                                    
                                    editor = page.locator('div[data-testid="tweetTextarea_0"]').first
                                    if editor.is_visible():
                                        editor.click()
                                        time.sleep(0.3)
                                        page.keyboard.type(reply_content, delay=10)
                                        time.sleep(1.5)
                                        
                                        post_reply_btn = page.locator('[data-testid="tweetButton"]').first
                                        if post_reply_btn.is_enabled():
                                            post_reply_btn.click()
                                            time.sleep(6)
                                            record_reply(tweet_id, author, text[:200], reply_content)
                                            replies_done.append({
                                                "author": author,
                                                "tweet": text[:120],
                                                "reply": reply_content
                                            })
                                            print(f"Reply #{len(replies_done)} posted successfully!", file=sys.stderr)
                        except Exception:
                            continue
                            
                    try:
                        page.mouse.wheel(0, 1500)
                        time.sleep(2.5)
                    except Exception:
                        pass
                
            context.close()
        except Exception as e:
            print(f"Error in scan_and_reply: {e}", file=sys.stderr)
            
    return replies_done

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Reply to high-reach technical tweets.')
    # Positional count kept so the existing cron invocation still works.
    parser.add_argument('max_replies', nargs='?', type=int, default=3,
                        help='maximum replies to post in one run (default 3)')
    parser.add_argument('--dry-run', action='store_true',
                        help='scan and draft, but post nothing')
    args = parser.parse_args()

    max_count = args.max_replies
    try:
        with profile_lock('social_x_reply_outbound'):
            done = scan_and_reply(max_replies=max_count, dry_run=args.dry_run)
    except BrowserBusy as e:
        print(f"Skipping run: {e}", file=sys.stderr)
        done = []
    print(json.dumps(done, indent=2))
