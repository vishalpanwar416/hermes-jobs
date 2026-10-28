#!/usr/bin/env python3
"""X (Twitter) growth posting script — browser-based (no API keys needed)."""
import asyncio, os, re, json, sys, sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from playwright.async_api import async_playwright

HISTORY_FILE = Path.home() / ".hermes" / "scripts" / "x_post_history.json"
# Post tracking lived only in the JSON history above, which x_metrics.py cannot
# read, so posts_published has been reporting 0 whatever happened. Same database
# as the replies so a day's activity can be counted in one query.
GROWTH_DB = Path.home() / ".hermes" / "data" / "x_growth.db"

# Load .env
ENV_FILE = Path.home() / ".hermes" / ".env"
if ENV_FILE.exists():
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            key = key.strip(); val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val

POSTS = [
    {
        "id": "go_backend",
        "text": """If I were learning Go for backend development from scratch today, I wouldn't start with frameworks. I'd build projects that teach you what production actually needs.

1. Build a REST API
HTTP router → JSON handling → Middleware → PostgreSQL → Struct validation

2. Build a CLI tool
Flags → File I/O → Goroutines → Channels → Error handling patterns

3. Build a CRUD backend
Repository pattern → Migrations → Connection pooling → Graceful shutdown

4. Build a rate-limited API
Middleware → Redis counters → TTL → Context cancellation

5. Build a production service
Logging → Metrics → Graceful degradation → Health checks → Docker

The goal isn't to know every Go library. It's to understand what happens when your service handles 1K requests per second.
"""
    },
    {
        "id": "kubernetes",
        "text": """If I were learning Kubernetes from scratch today, I wouldn't start with YAML templates. I'd understand what happens when a pod dies.

1. Deploy a container
Image → Pod → Service → Ingress → See it work

2. Break it on purpose
Kill a pod → Watch it restart → Understand liveness probes

3. Scale it
Replicas → HPA → Load testing → Watch it autoscale

4. Add config
ConfigMaps → Secrets → Environment injection → Remove hardcoded values

5. Deploy properly
Rolling updates → Rollbacks → Resource limits → Namespace isolation

The goal isn't to memorize YAML syntax. It's to understand what happens when your app goes down at 2am.
"""
    },
    {
        "id": "system_design",
        "text": """If I were learning system design from scratch today, I wouldn't start with diagrams. I'd build systems that force you to make real tradeoffs.

1. Build a URL shortener
Hash generation → Database → Cache → Redirect → Handle 1M+ keys

2. Build a chat system
WebSockets → Message queue → Persistence → Fan-out → Handle 10K concurrent connections

3. Build a rate limiter
Sliding window → Redis → Distributed counters → Handle traffic spikes

4. Build a notification system
Event bus → Workers → Retry with backoff → Dead letter queue → Delivery guarantees

5. Build a payment system
Idempotency keys → Two-phase commit → Retry safety → Audit logs → Handle failure gracefully

The goal isn't to draw boxes and arrows. It's to understand why every tradeoff has a cost.
"""
    },
    {
        "id": "redis",
        "text": """If I were learning Redis from scratch today, I wouldn't start with commands. I'd build things that break without caching.

1. Build a session store
SET/GET → TTL → Serialization → Handle 100K concurrent logins

2. Build a rate limiter
Sliding window counter → Lua scripts → Atomic operations → Distributed safety

3. Build a message queue
LPUSH/BRPOP → Worker pattern → Acknowledgment → Failure handling

4. Build a real-time leaderboard
Sorted sets → ZADD/ZRANGE → Score updates → 1M+ entries

5. Build a cache layer
Cache aside → Write through → Invalidation → Thundering herd problem

The goal isn't to memorize Redis commands. It's to understand what happens when your database falls over.
"""
    },
    {
        "id": "postgresql",
        "text": """If I were learning PostgreSQL from scratch today, I wouldn't start with ORMs. I'd build queries that teach you how databases actually work.

1. Build a schema
Tables → Types → Constraints → Indexes → Relations

2. Write queries
SELECT → JOIN → Aggregation → Subqueries → CTEs

3. Optimize slow queries
EXPLAIN ANALYZE → Index scan vs seq scan → Query planning → Why it's slow

4. Handle concurrency
Transactions → Isolation levels → Deadlocks → FOR UPDATE → MVCC

5. Scale it
Read replicas → Connection pooling → Partitioning → Backup strategies

The goal isn't to memorize SQL syntax. It's to understand what happens when your query takes 30 seconds.
"""
    }
]

QUEUE_FILE = Path.home() / ".hermes" / "data" / "x_content_queue.json"

# Which kind of post belongs on which weekday. See X_GROWTH_PLAN.md section 4.
# Fri/Sat are deliberately empty — five posts a week beats seven with filler.
WEEKDAY_KIND = {
    0: "build_log",   # Mon
    1: "teardown",    # Tue
    2: "build_log",   # Wed
    3: "opinion",     # Thu
    4: None,          # Fri
    5: None,          # Sat
    6: "longform",    # Sun
}

# The old hardcoded POSTS are all one format ("If I were learning X from
# scratch"). They still work as a floor when the queue runs dry, but capping
# them at once a fortnight stops them being the whole timeline again.
LEARNING_PATH_MIN_GAP_DAYS = 14


def load_history():
    if HISTORY_FILE.exists():
        return json.loads(HISTORY_FILE.read_text())
    return {"posts": [], "last_post_id": None}

def save_history(history):
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_FILE.write_text(json.dumps(history, indent=2))

def record_post(post_id, kind, text):
    """Write the published post to x_growth.db for x_metrics.py to count.

    Deliberately swallows every error. A post that went out and failed to get
    logged is a measurement problem; raising here would turn it into a job
    failure and, worse, a retry that posts the same thing twice.
    """
    try:
        conn = sqlite3.connect(GROWTH_DB)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id TEXT, kind TEXT, text TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )''')
        conn.execute('INSERT INTO posts (post_id, kind, text, created_at) VALUES (?,?,?,?)',
                     (post_id, kind, text, datetime.now().isoformat(timespec='seconds')))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[posts] could not record to x_growth.db: {e}", file=sys.stderr)

def posted_today():
    """True if a post already went out today — the retry job's guard.

    Checks both records, because they can disagree. The JSON history is written
    first and the database second, so a crash between them leaves a published
    post visible in only one. Either one counts: the cost of a false positive is
    a skipped post, the cost of a false negative is posting twice.
    """
    today = datetime.now().date()
    try:
        conn = sqlite3.connect(GROWTH_DB)
        row = conn.execute(
            "SELECT 1 FROM posts WHERE date(created_at)=date('now','localtime') LIMIT 1"
        ).fetchone()
        conn.close()
        if row:
            return True
    except Exception:
        pass
    try:
        for entry in load_history().get("posts", []):
            if datetime.fromisoformat(entry["date"]).date() == today:
                return True
    except Exception:
        pass
    return False

def load_queue():
    if QUEUE_FILE.exists():
        try:
            return json.loads(QUEUE_FILE.read_text()).get("posts", [])
        except Exception as e:
            print(f"[queue] unreadable, ignoring: {e}", file=sys.stderr)
    return []

def mark_queue_used(post_id):
    if not QUEUE_FILE.exists():
        return
    try:
        data = json.loads(QUEUE_FILE.read_text())
        for p in data.get("posts", []):
            if p.get("id") == post_id:
                p["status"] = "used"
                p["used_at"] = datetime.now().isoformat(timespec="seconds")
        QUEUE_FILE.write_text(json.dumps(data, indent=2))
    except Exception as e:
        print(f"[queue] could not mark used: {e}", file=sys.stderr)

def _recent_ids(history, days):
    cutoff = datetime.now().date() - timedelta(days=days)
    out = set()
    for entry in history.get("posts", []):
        try:
            if datetime.fromisoformat(entry["date"]).date() >= cutoff:
                out.add(entry["post_id"])
        except Exception:
            pass
    return out

def _last_learning_path_days_ago(history):
    ids = {p["id"] for p in POSTS}
    newest = None
    for entry in history.get("posts", []):
        if entry.get("post_id") in ids:
            try:
                d = datetime.fromisoformat(entry["date"]).date()
                newest = d if newest is None or d > newest else newest
            except Exception:
                pass
    if newest is None:
        return 999
    return (datetime.now().date() - newest).days

def pick_post(history):
    """Queue first, matched to the weekday's slot. Learning paths are the floor.

    Returns None when today has no slot, or when the queue is dry and a
    learning-path post would breach the fortnight cap. None means post nothing —
    a skipped day is cheaper than filler.
    """
    kind = WEEKDAY_KIND.get(datetime.now().weekday())
    if kind is None:
        print(f"[pick] no slot for {datetime.now():%A} — skipping", file=sys.stderr)
        return None

    recent = _recent_ids(history, 7)

    ready = [p for p in load_queue()
             if p.get("status") == "ready" and p.get("id") not in recent
             and (p.get("tweets") or p.get("text"))]
    if ready:
        # Prefer the weekday's kind, but any ready post beats falling back to a
        # learning path — the queue is the whole point of the new mix.
        for p in ready:
            if p.get("kind") == kind:
                return p
        print(f"[pick] no '{kind}' queued, using next ready post", file=sys.stderr)
        return ready[0]

    gap = _last_learning_path_days_ago(history)
    if gap < LEARNING_PATH_MIN_GAP_DAYS:
        print(f"[pick] queue empty and last learning-path post was {gap}d ago "
              f"(cap {LEARNING_PATH_MIN_GAP_DAYS}d) — skipping", file=sys.stderr)
        return None

    for post in POSTS:
        if post["id"] not in _recent_ids(history, 30):
            return post
    return POSTS[0]

# ---------------------------------------------------------------------------
# Length. The account is X Free: 280 weighted characters per post, no long-form.
# ---------------------------------------------------------------------------
#
# Discovered the hard way. Every entry in the content queue was 450 to 975
# characters. The old post_to_x() dropped the whole text into the composer,
# clicked Post, slept four seconds and printed "Posted successfully" — and
# recorded the post in two places. X had silently refused all of them; the
# timeline on 2026-09-10 showed nothing newer than 28 August while the history
# said three posts had gone out that week. So: count the way X counts, split
# into a thread when needed, and treat "posted" as something to verify, not
# assume.

X_LIMIT = 280
# Target per tweet, below the limit, so a stray emoji or X's own counting quirks
# cannot push a tweet over after we have already committed to the split.
TWEET_TARGET = 270
MAX_THREAD = 6

_URL_RE = re.compile(r'https?://\S+|\b[\w.-]+\.(?:com|dev|io|in|net|org|ai|fm|sh)\b\S*')


def x_len(text):
    """Weighted length as X's composer counts it (close enough for a 10-char margin).

    Every URL counts 23 regardless of length. Characters outside the basic
    ranges (emoji, CJK, most symbols) count 2. Everything else counts 1.
    """
    total = 0
    pos = 0
    for m in _URL_RE.finditer(text):
        total += _plain_len(text[pos:m.start()]) + 23
        pos = m.end()
    total += _plain_len(text[pos:])
    return total


def _plain_len(s):
    n = 0
    for ch in s:
        o = ord(ch)
        n += 1 if (o < 0x1100 or 0x2000 <= o <= 0x200F or 0x2010 <= o <= 0x201F) else 2
    return n


def split_thread(text, target=TWEET_TARGET):
    """Split prose into tweets at paragraph, then sentence, then word boundaries.

    Paragraph breaks are kept inside a tweet when two short paragraphs fit
    together. Nothing is numbered: the queued posts are conversational and a
    "1/" prefix reads as a marketing thread. If the text already fits, one tweet.
    """
    text = text.strip()
    if x_len(text) <= target:
        return [text]

    def pack(units, sep):
        out, cur = [], ''
        for u in units:
            cand = (cur + sep + u) if cur else u
            if x_len(cand) <= target:
                cur = cand
            else:
                if cur:
                    out.append(cur)
                cur = u
        if cur:
            out.append(cur)
        return out

    paragraphs = [p.strip() for p in text.split('\n\n') if p.strip()]
    tweets = []
    for chunk in pack(paragraphs, '\n\n'):
        if x_len(chunk) <= target:
            tweets.append(chunk)
            continue
        sentences = [s for s in re.split(r'(?<=[.!?])\s+', chunk) if s]
        for piece in pack(sentences, ' '):
            if x_len(piece) <= target:
                tweets.append(piece)
            else:
                tweets.extend(pack(piece.split(' '), ' '))
    return tweets


def tweets_for(post):
    """The tweets a queue entry publishes as.

    Prefer an explicit "tweets" list — that is a human or the drafting agent
    deciding where the breaks go, which is always better than the splitter. Fall
    back to splitting "text". Either way, every tweet is checked against the
    hard limit here, so nothing over 280 can reach the composer.
    """
    explicit = post.get("tweets")
    tweets = [t.strip() for t in explicit if t and t.strip()] if explicit else split_thread(post["text"])
    bad = [(i + 1, x_len(t)) for i, t in enumerate(tweets) if x_len(t) > X_LIMIT]
    if bad:
        raise ValueError(f"tweets over {X_LIMIT}: " + ', '.join(f'#{i}={n}' for i, n in bad))
    if len(tweets) > MAX_THREAD:
        raise ValueError(f"{len(tweets)} tweets, max {MAX_THREAD}")
    return tweets


def validate_queue():
    """Report every queue entry that cannot publish as-is. Exit 1 if any.

    Run by the publish job's agent after it tops the queue up, so a 500-char
    paragraph gets rejected at write time instead of at 20:30.
    """
    problems = 0
    for p in load_queue():
        if p.get("status") not in ("ready", "draft"):
            continue
        try:
            tweets = tweets_for(p)
        except (ValueError, KeyError) as e:
            problems += 1
            print(f"  ✗ {p.get('id')}: {e}")
            continue
        src = 'explicit' if p.get('tweets') else 'auto-split'
        lens = ', '.join(str(x_len(t)) for t in tweets)
        flag = ''
        if not p.get('tweets') and len(tweets) > 1:
            flag = '   <- auto-split; write "tweets" by hand for a better first tweet'
        print(f"  ✓ {p.get('id'):22} {p.get('status'):5} {len(tweets)} tweet(s) [{lens}] {src}{flag}")
    if problems:
        print(f"{problems} entr{'y' if problems == 1 else 'ies'} cannot publish")
    return problems == 0


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------

PROFILE_PATH = os.path.expanduser("~/.hermes/data/chrome_automation_profile")
OWN_HANDLE = "VishalPanwarr"


async def _type_tweet(page, index, text):
    sel = f'div[data-testid="tweetTextarea_{index}"]'
    await page.wait_for_selector(sel, timeout=15000)
    editor = page.locator(sel).first
    await editor.click(force=True)
    await asyncio.sleep(0.3)
    # Keyboard input, not execCommand: the editor's state only updates from real
    # key events, and it is that state which decides whether Post is enabled and
    # what the character counter says. Newlines become paragraph breaks.
    await page.keyboard.type(text, delay=4)
    await asyncio.sleep(0.6)


async def _verify_on_profile(page, first_tweet):
    """Find the freshly posted tweet on the profile; return its URL or None."""
    needle = ' '.join(first_tweet.split())[:60].lower()
    try:
        await page.goto(f"https://x.com/{OWN_HANDLE}", timeout=45000, wait_until="domcontentloaded")
        await asyncio.sleep(5)
        arts = page.locator('article[data-testid="tweet"]')
        n = min(await arts.count(), 8)
        for i in range(n):
            try:
                body = ' '.join((await arts.nth(i).inner_text(timeout=2000)).split()).lower()
            except Exception:
                continue
            if needle in body:
                href = await arts.nth(i).locator('a[href*="/status/"]').first.get_attribute('href', timeout=2000)
                return ('https://x.com' + href) if href and href.startswith('/') else href
    except Exception as e:
        print(f"[verify] profile check failed: {e}", file=sys.stderr)
    return None


async def post_to_x(tweets, dry_run=False):
    """Publish `tweets` as a single post or a thread. Returns the post URL, or None.

    Success means the tweet was found on the profile afterwards. A click that
    produced no visible post is a failure, whatever the composer looked like.
    """
    for lock in ['SingletonLock', 'SingletonSocket', 'SingletonCookie']:
        f = os.path.join(PROFILE_PATH, lock)
        if os.path.exists(f):
            try: os.remove(f)
            except: pass

    # Headless by default: the gateway's session has a DISPLAY, which used to
    # put a Chrome window on screen every evening. HERMES_HEADED=1 to watch it.
    headless = os.environ.get("HERMES_HEADED") != "1"
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=PROFILE_PATH,
            headless=headless,
            channel="chrome",
            args=["--no-sandbox", "--disable-gpu", "--start-maximized",
                  "--disable-blink-features=AutomationControlled"],
            no_viewport=not headless,
            viewport={"width": 1440, "height": 1000} if headless else None,
        )
        page = context.pages[0] if context.pages else await context.new_page()

        await page.goto("https://x.com/compose/post", timeout=30000, wait_until="domcontentloaded")

        # A logged-out session is redirected to https://x.com/ (no "login" in the URL),
        # so detect it by the absence of the account nav rather than by URL matching.
        try:
            await page.wait_for_selector('div[data-testid="tweetTextarea_0"]', timeout=45000)
        except Exception:
            logged_in = await page.locator('[data-testid="SideNav_AccountSwitcher_Button"]').count() > 0
            print(f"Compose URL: {page.url[:80]}")
            if not logged_in:
                print("❌ AUTH ERROR: X session is logged out in "
                      f"{PROFILE_PATH} (no auth_token cookie). "
                      "Log in once in that profile to restore it.")
            else:
                print("❌ Composer did not load (logged in, but tweetTextarea_0 never appeared)")
            await context.close()
            return None

        for i, t in enumerate(tweets):
            if i > 0:
                # The "Add post" button only exists while the focused editor has
                # text, and it is rendered once, next to that editor. Wait for it
                # to appear rather than clicking whatever is there.
                added = False
                for attempt in range(3):
                    try:
                        add = page.locator('[data-testid="addButton"]:visible').last
                        await add.wait_for(state="visible", timeout=6000)
                        await add.scroll_into_view_if_needed()
                        await add.click(timeout=6000)
                        await page.wait_for_selector(f'div[data-testid="tweetTextarea_{i}"]',
                                                     timeout=8000)
                        added = True
                        break
                    except Exception:
                        # Re-focus the previous editor's end so the button re-renders.
                        try:
                            prev = page.locator(f'div[data-testid="tweetTextarea_{i - 1}"]').first
                            await prev.click(force=True)
                            await page.keyboard.press("End")
                        except Exception:
                            pass
                        await asyncio.sleep(1.0)
                if not added:
                    print(f"❌ Could not add tweet slot {i + 1} (addButton never became clickable)")
                    await context.close()
                    return None
                await asyncio.sleep(0.5)
            await _type_tweet(page, i, t)
            print(f"  typed tweet {i + 1}/{len(tweets)} ({x_len(t)} chars)")

        # The composer disables Post while any tweet is over the limit or empty.
        try:
            await page.wait_for_function(
                '() => { const b = document.querySelector(\'button[data-testid="tweetButton"]\');'
                ' return b && !b.disabled && b.getAttribute("aria-disabled") !== "true"; }',
                timeout=8000)
        except Exception:
            print("❌ Post button stayed disabled after typing; a tweet is over the limit or empty")
            await context.close()
            return None

        if dry_run:
            print(f"DRY RUN: {len(tweets)} tweet(s) entered and Post is enabled; not clicking.")
            await context.close()
            return "dry-run"

        await page.locator('button[data-testid="tweetButton"]').first.click(force=True)

        # The composer closes on success. If it is still open after ten seconds,
        # X refused the post (the toast says why) and nothing went out.
        try:
            await page.wait_for_selector('div[data-testid="tweetTextarea_0"]', state="detached",
                                         timeout=10000)
        except Exception:
            toast = ''
            try:
                toast = await page.locator('[data-testid="toast"]').first.inner_text(timeout=1500)
            except Exception:
                pass
            print(f"❌ Composer still open after Post: X did not accept it. {toast!r}")
            await context.close()
            return None

        await asyncio.sleep(3)
        url = await _verify_on_profile(page, tweets[0])
        await context.close()
        if not url:
            print("❌ Composer closed but the post is not on the profile. Not recording it.")
            return None
        print(f"✅ Posted: {url}")
        return url


async def main():
    if "--validate" in sys.argv:
        ok = validate_queue()
        sys.exit(0 if ok else 1)

    dry_run = "--dry-run" in sys.argv
    # The retry job runs this again after a failure at the main slot. If the
    # first attempt actually got a post out, stop — a double post is a worse
    # outcome than a missed one.
    if not dry_run and posted_today():
        print("[SILENT] already posted today")
        return
    history = load_history()
    post = pick_post(history)
    if post is None:
        print("[SILENT] no post scheduled for today")
        return
    print(f"Selected post: {post['id']} ({post.get('kind', 'learning_path')})")

    try:
        tweets = tweets_for(post)
    except ValueError as e:
        print(f"❌ {post['id']} cannot publish: {e}. Fix it in the queue (run --validate).")
        sys.exit(1)
    print(f"Publishing as {len(tweets)} tweet(s): " + ', '.join(str(x_len(t)) for t in tweets))
    for i, t in enumerate(tweets):
        print(f"  [{i + 1}] {t[:90]!r}")

    url = await post_to_x(tweets, dry_run=dry_run)
    if dry_run:
        return
    if url:
        history.setdefault("posts", []).append({
            "post_id": post["id"],
            "kind": post.get("kind", "learning_path"),
            "date": datetime.now().isoformat(),
            "url": url,
            "tweets": len(tweets),
            "text_preview": tweets[0][:80]
        })
        history["last_post_id"] = post["id"]
        save_history(history)
        mark_queue_used(post["id"])
        record_post(post["id"], post.get("kind", "learning_path"), '\n\n'.join(tweets))
        print(f"posted_url: {url}")
        print(f"content_preview: {tweets[0][:120]}")
        print(f"✅ History saved. Total posts: {len(history['posts'])}")
    else:
        print("❌ Post failed")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())