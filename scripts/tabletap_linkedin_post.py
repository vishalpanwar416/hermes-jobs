"""Post to the Table Tap LinkedIn Page.

Uses the already logged-in Chrome automation profile rather than the LinkedIn
API, which would need an approved Marketing Developer Platform app.

The Page is posted to in ADMIN context, so the author is Table Tap, not Vishal.
That is the whole point: posting from his personal profile would reach his
network instead of the Page's followers.

Voice comes from Table-Tap media/brand.json, which is a different voice from
Vishal's personal one in social_voice. The de-AI scrubber IS shared, because
dashes, hashtag blocks and "not just X, it's Y" read as machine-written whoever
is speaking.

Usage:
    python tabletap_linkedin_post.py --dry-run     # draft only, post nothing
    python tabletap_linkedin_post.py               # draft and publish
    python tabletap_linkedin_post.py --text "..."  # publish exact text
"""

import os
import re
import sys
import json
import time
import random
import sqlite3
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# Capture this run's full stdout and stderr to
# ~/.hermes/logs/pipelines/tabletap_linkedin_post/ so a failed or silent run can be
# diagnosed afterwards instead of vanishing.
if __name__ == '__main__':
    # Only when run directly. Firing on import made every script that
    # imports this module log its own run under this pipeline's name.
    try:
        import pipeline_log as _plog
        _plog.start('tabletap_linkedin_post')
    except Exception:
        pass

import social_voice as voice
from browser_lock import profile_lock, BrowserBusy

from playwright.sync_api import sync_playwright

PAGE_ID = '137084490'
# The composer lives on the page-posts view, not the admin root. Loading
# /admin/ alone finds no "Start a post" trigger at all.
ADMIN_URL = f'https://www.linkedin.com/company/{PAGE_ID}/admin/page-posts/published/'
BRAND_PATH = os.path.expanduser(
    '~/Development/Aarambh/Table-Tap media/brand.json')
HISTORY_DB = os.path.expanduser('~/.hermes/data/x_growth.db')

PROFILE_DIR = os.path.expanduser('~/.hermes/data/chrome_automation_profile')
MEDIA_ROOT = os.path.expanduser('~/Development/Aarambh/Table-Tap media')
QUEUE_DIR = os.path.join(MEDIA_ROOT, 'content', 'queue')
PUBLISHED_DIR = os.path.join(MEDIA_ROOT, 'content', 'published')


def pick_queued_item():
    """Oldest queued poster from the Table Tap media generator.

    The generator writes poster.png alongside post.json holding the concept it
    drew. Drafting the LinkedIn text from that same concept keeps the words and
    the picture about the same thing, instead of pairing an unrelated poster
    with unrelated copy.
    """
    if not os.path.isdir(QUEUE_DIR):
        return None
    for name in sorted(os.listdir(QUEUE_DIR)):
        d = os.path.join(QUEUE_DIR, name)
        poster = os.path.join(d, 'poster.png')
        meta = os.path.join(d, 'post.json')
        if os.path.isfile(poster) and os.path.isfile(meta):
            try:
                with open(meta) as fh:
                    data = json.load(fh)
            except (OSError, ValueError):
                data = {}
            return {'dir': d, 'poster': poster, 'meta': data, 'id': name}
    return None


def generate_poster(timeout_s=300):
    """Ask the Table Tap media generator for a fresh poster.

    Without this the queue drains after one post and every later run silently
    falls back to text only, which is the weakest version of the post.
    """
    import subprocess
    try:
        r = subprocess.run(['node', 'src/index.js', 'generate'],
                           cwd=MEDIA_ROOT, capture_output=True, text=True,
                           timeout=timeout_s)
        if r.returncode != 0:
            print(f'[gen] generator failed: {(r.stderr or r.stdout)[-200:]}',
                  file=sys.stderr)
            return False
        return True
    except Exception as e:
        print(f'[gen] generator error: {str(e)[:120]}', file=sys.stderr)
        return False


def brand_poster_file(poster_path):
    """Stamp the real logo on before the poster goes out.

    Idempotent: brand_poster writes a .branded marker and skips already-branded
    files, so calling it on a queue item that was branded at generation time is
    a no-op rather than a double stamp.
    """
    try:
        from brand_poster import brand_one
        res = brand_one(poster_path)
        print(f"[brand] {res.get('action', res.get('error'))}", file=sys.stderr)
        return not res.get('error')
    except Exception as e:
        print(f'[brand] skipped: {str(e)[:110]}', file=sys.stderr)
        return False


def archive_item(item):
    """Move a used queue item into published/ so it is not posted twice."""
    if not item:
        return
    try:
        os.makedirs(PUBLISHED_DIR, exist_ok=True)
        os.rename(item['dir'], os.path.join(PUBLISHED_DIR, item['id']))
    except OSError as e:
        print(f'[warn] could not archive {item["id"]}: {e}', file=sys.stderr)


def load_brand():
    with open(BRAND_PATH) as fh:
        return json.load(fh)


def _history_conn():
    conn = sqlite3.connect(HISTORY_DB, timeout=10)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS tabletap_li_posts (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            pillar     TEXT,
            content    TEXT UNIQUE,
            status     TEXT DEFAULT 'posted',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    return conn


def recent_posts(limit=10):
    conn = _history_conn()
    try:
        return [r[0] for r in conn.execute(
            'SELECT content FROM tabletap_li_posts ORDER BY id DESC LIMIT ?',
            (limit,)).fetchall()]
    finally:
        conn.close()


def record_post(pillar, content, status='posted'):
    conn = _history_conn()
    try:
        conn.execute(
            'INSERT OR IGNORE INTO tabletap_li_posts (pillar, content, status) '
            'VALUES (?, ?, ?)', (pillar, content, status))
        conn.commit()
    finally:
        conn.close()


def facts_block(brand):
    """A rotating sample of real product facts scraped from table-tap.in.

    Sampled per run so consecutive posts lean on different features, but the
    model is never allowed to invent product claims of its own.
    """
    facts = brand.get('productFacts')
    if not facts:
        return ''
    picked = random.sample(facts['features'], min(4, len(facts['features'])))
    picked += random.sample(facts['numbers'], min(2, len(facts['numbers'])))
    lines = '\n'.join(f'- {f}' for f in picked)
    return f"""

Real product facts from {brand['website']}. These are the ONLY product claims,
features and numbers you may use. Do not invent features, integrations or
statistics that are not listed here:
- Latest release, {facts['latestRelease']}
{lines}
- {facts['positioning']}

Build the post around the content pillar using at most one or two of these
facts. Do not cram in a list of features."""


def draft_post(brand, item=None):
    """Write one LinkedIn post in the Table Tap brand voice."""
    pillar = random.choice(brand['contentPillars'])
    if item and item['meta'].get('pillar'):
        match = [p for p in brand['contentPillars']
                 if p['id'] == item['meta']['pillar']]
        if match:
            pillar = match[0]
    avoid = recent_posts()
    avoid_block = ''
    if avoid:
        joined = '\n---\n'.join(p[:180] for p in avoid)
        avoid_block = ('\n\nAlready posted recently. Do not repeat these topics, '
                       f'openings or structure:\n{joined}')

    system = f"""You write LinkedIn posts for {brand['name']}, {brand['oneLiner']}
Website: {brand['website']}
Audience: {brand['audience']}
Brand voice: {brand['voice']}

This is LinkedIn, not Instagram. That means:
- No hashtag blocks. At most two hashtags, at the end, or none.
- Speak to restaurant owners and operators as a peer who knows the operational
  detail: table turnover, order errors, staff shortage, printed menu costs.
- One concrete specific point per post. A number or a real scenario beats
  adjectives.
- Three to six short lines. Line breaks between thoughts, LinkedIn rewards
  scannable posts.
- No dashes as punctuation. No em dash, no en dash, no " - " joining clauses.
  Use a comma, a full stop, or split the sentence.
- Never open with "In today's fast-paced world" or a rhetorical question.
- Never use: delve, leverage, robust, seamless, elevate, unlock, harness,
  game changer, revolutionise, transform your business.
- No emoji, or at most one.
- End with a light call to action pointing at {brand['website']} only when it
  fits. Do not force it every time.

Hard limit 900 characters.{facts_block(brand)}"""

    task = (f"Write one LinkedIn post for the {brand['name']} company page.\n"
            f"Content pillar: {pillar['theme']}")
    if item and item['meta'].get('title'):
        task += (f"\nThe post carries an image titled \"{item['meta']['title']}\". "
                 f"Write copy that belongs with that image. Do not describe the "
                 f"image, and do not repeat its headline verbatim.")

    text = voice.draft(task=task, context=f"Write the post now.{avoid_block}",
                       limit=900, temperature=0.9, allow_skip=False)
    if not text or text is voice.SKIP:
        return None, pillar['id']
    return text, pillar['id']


def attach_image(page, poster_path):
    """Attach a poster to the open composer.

    LinkedIn mounts no input[type=file] at all until a media button is clicked,
    and clicking one opens a NATIVE file chooser, which set_input_files cannot
    reach. Playwright can intercept that chooser, so drive the real button and
    catch the dialog it raises.
    """
    triggers = [
        'button[aria-label="Add a photo"]',
        'button[aria-label="Add media"]',
        'button:has-text("Photo")',
    ]
    for sel in triggers:
        loc = page.locator(sel).first
        try:
            if not loc.count() or not loc.is_visible():
                continue
            with page.expect_file_chooser(timeout=15000) as fc:
                loc.click()
            fc.value.set_files(poster_path)
            page.wait_for_timeout(7000)
            # LinkedIn shows a Next/Done step after the crop preview.
            for label in ('Next', 'Done'):
                btn = page.locator(f'button:has-text("{label}")').first
                if btn.count() and btn.is_visible() and not btn.is_disabled():
                    btn.click()
                    page.wait_for_timeout(3500)
                    break
            return True, f'image attached via {sel}'
        except Exception as e:
            print(f'[image] {sel} failed: {str(e)[:70]}', file=sys.stderr)
            continue
    return False, 'no media trigger accepted the file'


def publish(page, text, poster_path=None):
    """Post as the Page from its admin view."""
    page.goto(ADMIN_URL, timeout=45000, wait_until='domcontentloaded')
    time.sleep(6)

    if '/admin' not in page.url:
        return False, f'not in admin context, landed on {page.url}', False

    starter = None
    for sel in ['button:has-text("Start a post")',
                'button:has-text("Create a post")',
                '.share-box-feed-entry__trigger',
                'button.artdeco-button:has-text("post")']:
        loc = page.locator(sel).first
        if loc.count() and loc.is_visible():
            starter = loc
            break
    if starter is None:
        return False, 'post composer trigger not found', False
    starter.click()
    time.sleep(4)

    editor = None
    for sel in ['div.ql-editor[contenteditable="true"]',
                'div[role="textbox"][contenteditable="true"]',
                'div.editor-content div[contenteditable="true"]']:
        loc = page.locator(sel).first
        if loc.count() and loc.is_visible():
            editor = loc
            break
    if editor is None:
        return False, 'composer editor not found', False

    image_ok = False
    if poster_path:
        image_ok, img_detail = attach_image(page, poster_path)
        print(f'[image] {img_detail}', file=sys.stderr)
        # Re-resolve the editor: attaching media re-renders the composer, so the
        # handle taken before the upload can be detached.
        for sel in ['div.ql-editor[contenteditable="true"]',
                    'div[role="textbox"][contenteditable="true"]']:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible():
                editor = loc
                break

    editor.click()
    time.sleep(0.5)
    for line in text.split('\n'):
        if line:
            page.keyboard.type(line, delay=12)
        page.keyboard.press('Shift+Enter')
        time.sleep(0.15)
    time.sleep(2)

    posted = page.evaluate("""() => {
      const btns=[...document.querySelectorAll('button')]
        .filter(b => b.offsetParent !== null && !b.disabled
                     && /^post$/i.test((b.textContent||'').trim()));
      if(!btns.length) return false;
      btns[0].scrollIntoView({block:'center'});
      btns[0].click();
      return true;
    }""")
    if not posted:
        return False, 'Post button not clickable', image_ok
    time.sleep(8)

    # Verify rather than trust the click.
    try:
        body = page.inner_text('body')
        probe = re.sub(r'\s+', ' ', text)[:50]
        if probe and re.sub(r'\s+', ' ', body).find(probe) >= 0:
            return True, 'posted and verified on page', image_ok
    except Exception:
        pass
    return True, 'post clicked, could not verify on page', image_ok


def run(dry_run=False, explicit_text=None, with_image=True):
    brand = load_brand()
    item = pick_queued_item() if with_image else None
    if with_image and item is None:
        print('[gen] poster queue empty, generating one', file=sys.stderr)
        if generate_poster():
            item = pick_queued_item()
    if item:
        brand_poster_file(item['poster'])
    if explicit_text:
        text, pillar = explicit_text, 'manual'
    else:
        text, pillar = draft_post(brand, item)
    if not text:
        return {'error': 'draft failed'}

    text = voice.humanize(text, limit=900)
    out = {'pillar': pillar, 'chars': len(text), 'text': text,
           'tells': voice.has_ai_tells(text),
           'image': (item['poster'] if item else None),
           'image_concept': (item['meta'].get('title') if item else None)}

    if dry_run:
        out['action'] = 'dry-run, nothing posted'
        return out

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR, channel='chrome', headless=True,
            args=['--disable-blink-features=AutomationControlled', '--no-sandbox'])
        page = ctx.new_page()
        page.set_viewport_size({'width': 1440, 'height': 900})
        ok, detail, image_ok = publish(page, text, item['poster'] if item else None)
        ctx.close()

    out['action'] = detail
    out['image_attached'] = image_ok
    if ok:
        record_post(pillar, text)
        # Only consume the poster if it actually went out with the post.
        # Archiving on post-success alone burned the queued poster on a run
        # that published text only.
        if item and image_ok:
            archive_item(item)
        elif item:
            print('[image] poster kept in queue, it was not attached',
                  file=sys.stderr)
    return out


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--text', help='publish this exact text instead of drafting')
    ap.add_argument('--no-image', action='store_true',
                    help='post text only, ignore the poster queue')
    args = ap.parse_args()

    if args.dry_run:
        print(json.dumps(run(dry_run=True, explicit_text=args.text,
                             with_image=not args.no_image),
                         indent=2, ensure_ascii=False))
    else:
        try:
            with profile_lock('tabletap_linkedin_post'):
                res = run(explicit_text=args.text, with_image=not args.no_image)
        except BrowserBusy as e:
            res = {'error': f'skipped: {e}'}
        print(json.dumps(res, indent=2, ensure_ascii=False))
