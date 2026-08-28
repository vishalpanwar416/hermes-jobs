import os
import sys
import time
import json
import sqlite3
import re
import hashlib
from playwright.sync_api import sync_playwright
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if __name__ == '__main__':
    # Only when run directly. Firing on import made every script that
    # imports this module log its own run under this pipeline's name.
    try:
        import pipeline_log as _plog
        _plog.start('social_inbound_reply_x_linkedin')
    except Exception:
        pass
try:
    import social_voice as voice
except Exception:
    voice = None
import shared_dedupe as dedupe
from browser_lock import profile_lock, BrowserBusy

DB_PATH = os.path.expanduser('~/.hermes/data/x_growth.db')
AUTOMATION_PROFILE_DIR = os.path.expanduser('~/.hermes/data/chrome_automation_profile')

def init_tables():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    # X inbound
    cur.execute('''
    CREATE TABLE IF NOT EXISTS inbound_replies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tweet_id TEXT UNIQUE NOT NULL,
        author TEXT,
        tweet_text TEXT,
        reply_text TEXT,
        platform TEXT DEFAULT 'x',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    # LinkedIn inbound
    cur.execute('''
    CREATE TABLE IF NOT EXISTS linkedin_inbound_replies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        notif_key TEXT UNIQUE NOT NULL,
        author TEXT,
        notif_text TEXT,
        reply_text TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    conn.commit()
    conn.close()

def is_inbound_replied(tweet_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id FROM inbound_replies WHERE tweet_id = ?", (tweet_id,))
    row = cur.fetchone()
    conn.close()
    return row is not None

def is_li_replied(notif_key):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id FROM linkedin_inbound_replies WHERE notif_key = ?", (notif_key,))
    row = cur.fetchone()
    conn.close()
    return row is not None

def _ensure_columns():
    """Add columns the live tables predate.

    CREATE TABLE IF NOT EXISTS silently does nothing when the table already
    exists, so a column added to the schema later never reaches an existing
    database and every insert naming it fails.
    """
    conn = sqlite3.connect(DB_PATH)
    try:
        cols = {r[1] for r in conn.execute('PRAGMA table_info(inbound_replies)')}
        if cols and 'platform' not in cols:
            conn.execute("ALTER TABLE inbound_replies ADD COLUMN platform TEXT DEFAULT 'x'")
            conn.commit()
            print('[migrate] added inbound_replies.platform', file=sys.stderr)
    except sqlite3.Error as e:
        print(f'[migrate] skipped: {e}', file=sys.stderr)
    finally:
        conn.close()


def record_reply(tweet_id, author, tweet_text, reply_text, platform='x'):
    _ensure_columns()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    if platform == 'x':
        cur.execute('''
        INSERT OR IGNORE INTO inbound_replies (tweet_id, author, tweet_text, reply_text, platform)
        VALUES (?, ?, ?, ?, 'x')
        ''', (tweet_id, author, tweet_text, reply_text))
    elif platform == 'linkedin':
        cur.execute('''
        INSERT OR IGNORE INTO linkedin_inbound_replies (notif_key, author, notif_text, reply_text)
        VALUES (?, ?, ?, ?)
        ''', (tweet_id, author, tweet_text, reply_text))
    conn.commit()
    conn.close()

def generate_contextual_response(author, text):
    t = text.lower()
    
    # 1. Questions / "Have you applied?", "Are you looking?", "Where can I find it?"
    if any(q in t for q in ['have you applied', 'did you apply', 'apply yet']):
        return "Checking out the details and applying now! Thanks for sharing."
    if any(q in t for q in ['where to find', 'link?', 'repo?', 'github?']):
        return "Here's the link: https://github.com/vishalpanwar416/multi-qr — feedback & PRs welcome!"
    if any(q in t for q in ['portfolio?', 'resume?', 'cv?']):
        return "You can check out my portfolio and live projects here: https://vishalpanwar.in"
    if any(q in t for q in ['open to work', 'looking for a job', 'available for roles']):
        return "Yes, actively looking for Backend / AI Engineer roles (Go, Node, K8s, GCP). Portfolio: https://vishalpanwar.in"
        
    # 2. Compliments / "Sounds fun", "Great project", "Awesome", "Love that"
    if any(c in t for c in ['sounds fun', 'love that', 'awesome', 'amazing', 'great job', 'cool project', 'nice one', 'good work']):
        return "Appreciate it! Glad you found it interesting."
        
    # 3. Technical questions about Go, latency, or caching
    if any(k in t for k in ['how did you', 'what stack', 'how do you']):
        if 'latency' in t or 'go' in t or 'p95' in t:
            return "Mainly buffer reuse with sync.Pool, streaming JSON encoders, and bounded worker pools to avoid GC spikes at high RPS."
        if 'qr' in t or 'wifi' in t:
            return "Built a custom codec that packs multiple action schemas into a compact binary-safe string before feeding it into the standard QR matrix."
        return "Built using Go microservices and React/Next.js with low-latency caching on GCP. Happy to share more details!"
        
    # 4. General positive acknowledgment
    if 'thanks' in t or 'thank you' in t:
        return "You're welcome! Let's connect."
        
    return "Thanks for the shoutout! Glad to connect."

def scan_and_reply_inbound(max_actions=3):
    init_tables()
    replies_sent = []
    
    with sync_playwright() as p:
        try:
            context = p.chromium.launch_persistent_context(
                user_data_dir=AUTOMATION_PROFILE_DIR,
                channel="chrome",
                headless=True,
                args=['--disable-blink-features=AutomationControlled', '--no-sandbox']
            )
            page = context.new_page()
            page.set_viewport_size({"width": 1440, "height": 900})
            
            print("Checking notifications tab for mentions and replies...", file=sys.stderr)
            page.goto("https://x.com/notifications/mentions", timeout=45000, wait_until="domcontentloaded")
            time.sleep(5)
            
            # If mentions tab empty, fallback to all notifications
            notif_items = page.locator('article[data-testid="tweet"]').all()
            if not notif_items:
                page.goto("https://x.com/notifications", timeout=45000, wait_until="domcontentloaded")
                time.sleep(5)
                notif_items = page.locator('article[data-testid="tweet"]').all()
                
            print(f"Found {len(notif_items)} notification items.", file=sys.stderr)
            
            # Harvest plain data from every notification BEFORE touching any of
            # them. Clicking navigates, which detaches every handle still held
            # from this snapshot, and the next item.inner_text() then hangs
            # until it times out. That is why this path failed on every run.
            mentions = []
            for item in notif_items:
                try:
                    text = item.inner_text()
                    if "@VishalPanwarr" not in text and "Replying to" not in text:
                        continue
                    tweet_url, author = "", ""
                    for l in item.locator('a').all():
                        href = l.get_attribute('href') or ''
                        if '/status/' in href and not tweet_url:
                            tweet_url = href
                        if href.startswith('/') and not href.startswith('/home') and '/status/' not in href and not author:
                            author = href.strip('/')
                    tweet_id = tweet_url.split('/status/')[-1].split('?')[0] if '/status/' in tweet_url else ""
                    if not tweet_id:
                        continue
                    mentions.append({
                        'text': text, 'author': author, 'tweet_id': tweet_id,
                        'url': f"https://x.com{tweet_url}" if tweet_url.startswith('/') else tweet_url,
                    })
                except Exception as e:
                    print(f"Skipped a notification while collecting: {e}", file=sys.stderr)
                    continue

            print(f"Collected {len(mentions)} mentions to consider.", file=sys.stderr)

            for m in mentions:
                if len(replies_sent) >= max_actions:
                    break
                try:
                    text, author, tweet_id = m['text'], m['author'], m['tweet_id']

                    if (is_inbound_replied(tweet_id)
                            or dedupe.already_answered('x', author, text)
                            or author in ['vishalpanwarr', 'VishalPanwarr', '']):
                        continue

                    response_text = generate_contextual_response(author, text)
                    if response_text:
                        print(f"Responding to @{author}: '{text[:60]}...' -> '{response_text}'", file=sys.stderr)

                        # Navigate by URL rather than clicking a snapshotted
                        # element, so the handle cannot be stale.
                        page.goto(m['url'], timeout=40000, wait_until="domcontentloaded")
                        time.sleep(3)
                        
                        target_tweet = page.locator('article[data-testid="tweet"]').filter(has_text=author).last
                        reply_btn = target_tweet.locator('[data-testid="reply"]').first
                        if reply_btn.is_visible():
                            reply_btn.click()
                            time.sleep(2)
                            
                            page.keyboard.type(response_text, delay=15)
                            time.sleep(1)
                            page.keyboard.press("Control+Enter")
                            time.sleep(6)
                            
                            record_reply(tweet_id, author, text[:200], response_text)
                            dedupe.mark_answered('x', author, text, response_text,
                                                 source='auto_reply_inbound')
                            replies_sent.append({
                                "author": author,
                                "received": text[:100],
                                "reply_sent": response_text
                            })
                            print("Inbound reply sent!", file=sys.stderr)
                except Exception as ex:
                    print(f"Error handling notification: {ex}", file=sys.stderr)
                    continue
                    
            context.close()
        except Exception as e:
            print(f"Error in scan_and_reply_inbound: {e}", file=sys.stderr)
            
    return replies_sent


def scan_linkedin_mentions(max_actions=3):
    """Scan LinkedIn notifications for mentions, comments, and replies on Vishal's posts."""
    init_tables()
    replies_sent = []

    with sync_playwright() as p:
        try:
            # Clean lingering locks
            for lock_name in ['SingletonLock', 'SingletonSocket', 'SingletonCookie']:
                lockfile = os.path.join(AUTOMATION_PROFILE_DIR, lock_name)
                if os.path.exists(lockfile):
                    try:
                        os.remove(lockfile)
                    except Exception:
                        pass

            context = p.chromium.launch_persistent_context(
                user_data_dir=AUTOMATION_PROFILE_DIR,
                channel="chrome",
                headless=True,
                args=['--disable-blink-features=AutomationControlled', '--no-sandbox',
                      '--disable-dev-shm-usage', '--disable-gpu']
            )
            page = context.new_page()
            page.set_viewport_size({"width": 1440, "height": 900})

            print("\n--- LinkedIn Inbound Scan ---", file=sys.stderr)
            page.goto("https://www.linkedin.com/notifications/", timeout=45000, wait_until="domcontentloaded")
            time.sleep(5)

            # LinkedIn notifications use aria-label="Notification" on each item
            notif_items = page.locator('[aria-label="Notification"]').all()
            if not notif_items:
                # Fallback: broad li/div grab
                notif_items = page.locator('[role="listitem"], li, div.occludable-update').all()
                notif_items = [n for n in notif_items if len(n.inner_text()) > 20]

            print(f"Found {len(notif_items)} LinkedIn notification items.", file=sys.stderr)

            for item in notif_items:
                if len(replies_sent) >= max_actions:
                    break
                try:
                    text = item.inner_text()[:500]

                    # Skip: own posts, job alerts, irrelevant noise
                    skip_signals = [
                        'congratulate', 'wish', 'birthday', 'work anniversary',
                        'job alert', 'recommended for you', 'suggested',
                        'skill assessment', 'premium', 'course',
                    ]
                    if any(s in text.lower() for s in skip_signals):
                        continue

                    # Only respond to comments/mentions on Vishal's posts
                    # LinkedIn notifications say things like:
                    #   "Someone commented: ..." or "Someone mentioned you"
                    # or are clearly a reply to Vishal's post ("Replying to Vishal Panwar")
                    low = text.lower()
                    # A reaction is not a conversation. These headlines contain
                    # 'commented'/'mentioned' and were slipping through, so the
                    # bot replied to people who had merely liked something.
                    if any(r in low for r in ('liked', 'reacted', 'celebrates',
                                              'loves', 'supports', 'finds this',
                                              'viewed your', 'follows you',
                                              'started following')):
                        continue

                    if not any(s in text.lower() for s in [
                        'commented', 'mentioned', 'reply', 'replied',
                        'vishal panwar', 'vishalpanwar',
                        'tagged you', 'shared with you',
                    ]):
                        continue

                    # Extract a stable key — use a hash of the text + position
                    notif_key = hashlib.md5(text.encode()).hexdigest()
                    if is_li_replied(notif_key):
                        continue

                    # Determine author from notification text
                    # Usually "FirstName LastName commented: ..."
                    author = "someone"
                    lines = [l.strip() for l in text.split('\n') if l.strip()]
                    if lines:
                        # The headline is a sentence ("Pranav Agarwal mentioned
                        # you in a comment."). Keep only the name in front of
                        # the verb, otherwise the reply is addressed to a
                        # sentence and the dedupe key is polluted by it.
                        head = lines[0]
                        m = re.split(r'\s+(?:mentioned|commented|replied|liked|'
                                     r'reacted|shared|posted|and\s+\d+\s+others?)\b',
                                     head, maxsplit=1, flags=re.I)
                        cand = (m[0] if m else head).strip(' .,:')
                        if 1 <= len(cand.split()) <= 4:
                            author = cand

                    # Cross-job check must come AFTER author is known: it used to
                    # sit above this block and raised UnboundLocalError on every
                    # LinkedIn notification, so this whole path always failed.
                    if dedupe.already_answered('linkedin', author, text):
                        continue

                    if author.lower() in ['vishal panwar', 'vishalpanwar', 'you']:
                        continue

                    # Use social_voice for reply generation if available
                    response_text = None
                    if voice is not None:
                        drafted = voice.draft_comment(author, text, platform='linkedin')
                        if drafted is not voice.SKIP:
                            response_text = drafted

                    if not response_text:
                        response_text = generate_contextual_response(author, text)

                    if response_text:
                        print(f"LI: Responding to {author}: '{text[:80]}...' -> '{response_text}'", file=sys.stderr)

                        # Click notification's anchor link to navigate to the post/comment
                        # The [aria-label="Notification"] element wraps everything;
                        # the actual link is an <a> inside it
                        link = item.locator('a').first
                        if link.is_visible(timeout=2000):
                            link.click()
                        else:
                            item.click()
                        time.sleep(4)

                        # LinkedIn post page — look for comment reply textarea
                        # Try multiple selectors that LinkedIn uses
                        reply_area = None
                        for sel in [
                            'div[contenteditable="true"]',
                            '.ql-editor[contenteditable]',
                            'textarea.comment-comment-box',
                            'div[role="textbox"]',
                        ]:
                            maybe = page.locator(sel).first
                            if maybe.is_visible(timeout=3000):
                                reply_area = maybe
                                break

                        if reply_area:
                            reply_area.click()
                            time.sleep(1)
                            page.keyboard.type(response_text, delay=15)
                            time.sleep(0.5)

                            # Press Enter or click submit button
                            submit_btn = page.locator('button.comment-comments-post-btn, button[data-control-name="comment"], button:has-text("Post")').first
                            if submit_btn.is_enabled(timeout=2000):
                                submit_btn.click()
                            else:
                                page.keyboard.press("Control+Enter")
                            time.sleep(4)

                            record_reply(notif_key, author, text[:300], response_text, platform='linkedin')
                            dedupe.mark_answered('linkedin', author, text, response_text,
                                                 source='auto_reply_inbound')
                            replies_sent.append({
                                "author": author,
                                "platform": "linkedin",
                                "received": text[:120],
                                "reply_sent": response_text
                            })
                            print("LinkedIn reply sent!", file=sys.stderr)

                        # Go back to notifications
                        page.goto("https://www.linkedin.com/notifications/", timeout=30000, wait_until="domcontentloaded")
                        time.sleep(3)

                except Exception as ex:
                    print(f"Error handling LinkedIn notification: {ex}", file=sys.stderr)
                    # Try to get back to notifications
                    try:
                        page.goto("https://www.linkedin.com/notifications/", timeout=30000, wait_until="domcontentloaded")
                        time.sleep(3)
                    except Exception:
                        pass
                    continue

            context.close()
        except Exception as e:
            print(f"Error in scan_linkedin_mentions: {e}", file=sys.stderr)

    return replies_sent


if __name__ == '__main__':
    init_tables()
    # One lock around BOTH scans: they drive the same Chrome profile, and
    # another scheduled job launching Chrome mid-run kills this one with
    # "Failed to create a ProcessSingleton for your profile directory".
    try:
        with profile_lock('social_inbound_reply_x_linkedin'):
            x_replies = scan_and_reply_inbound(max_actions=2)
            li_replies = scan_linkedin_mentions(max_actions=2)
            all_done = x_replies + li_replies
    except BrowserBusy as e:
        print(f"Skipping run: {e}", file=sys.stderr)
        all_done = []
    print(json.dumps(all_done, indent=2))
