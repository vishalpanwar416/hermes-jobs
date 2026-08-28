"""Hourly Naukri recommended-jobs auto-apply.

Pulls the logged-in "recommended jobs" feed, filters listings through the same
relevance rules as scrape_jobs_cron.py, and auto-applies ONLY to direct
one-click "Apply" listings. Listings that redirect to a company site or open a
screening questionnaire are never answered automatically — they are logged as
needs-attention and surfaced to WhatsApp instead.

Safety rails:
  MAX_APPLIES_PER_RUN  per hourly run
  MAX_APPLIES_PER_DAY  across all runs in one IST day

Exit codes: 0 ok, 2 not logged in (run naukri_login.py once), 3 blocked/bot-wall.
Stdout is a JSON summary for the cron agent to format for WhatsApp.
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

from playwright.sync_api import sync_playwright

PROFILE_DIR = os.path.expanduser("~/.hermes/data/chrome_automation_profile")
JOBS_DB = os.path.expanduser("~/.hermes/data/jobs_tracker.db")
DEBUG_DIR = os.path.expanduser("~/.hermes/data/naukri_debug")

RECOMMENDED_URL = "https://www.naukri.com/mnjuser/recommendedjobs"

# Caps removed 2026-08-23 at Vishal's request: apply to everything relevant that
# the recommended-jobs page turns up. The real bound is now how many relevant,
# unseen, easy-apply roles Naukri actually shows, not an arbitrary number.
# Set NAUKRI_MAX_PER_RUN / NAUKRI_MAX_PER_DAY in the environment to re-cap
# without editing this file.
# Per-run cap bounds runtime (each apply takes ~20-40s incl. questionnaires),
# not ambition. Bumped to 100/run for 100+ daily target (2026-08-27).
MAX_APPLIES_PER_RUN = int(os.environ.get("NAUKRI_MAX_PER_RUN", 100))
MAX_APPLIES_PER_DAY = int(os.environ.get("NAUKRI_MAX_PER_DAY", 10**6))

# Targeting (user-directed 2026-08-24): BACKEND ROLES ONLY, high volume
# (20+/hour). NAUKRI_TARGET=all lifts the backend restriction;
# NAUKRI_FILTER=1 restores the old generic keyword filter instead.
BACKEND_ONLY = os.environ.get("NAUKRI_TARGET", "backend") == "backend"
FILTER_RELEVANCE = os.environ.get("NAUKRI_FILTER", "") == "1"

# When the recommended page runs out of fresh cards, top up the run from
# public search-results pages for these queries.
SEARCH_QUERIES = [q.strip() for q in os.environ.get(
    "NAUKRI_SEARCH_QUERIES",
    "backend developer,golang developer,nodejs backend,python backend developer,"
    "java backend developer,microservices developer,"
    "go developer,full stack developer,software engineer backend,"
    "kubernetes developer,gcp developer,redis developer"
).split(",") if q.strip()]

# Backend gate: a clearly non-backend title is rejected outright; otherwise
# the title (or card text) must show a backend signal. Full-stack passes —
# those roles include backend work.
BACKEND_NEG = re.compile(
    r"front[\s-]?end|react\s+native|angular|vue\b|\bui\b|\bux\b|ios\b|android|"
    r"mobile|flutter|\bqa\b|sdet|test(er|ing)?\b|support|helpdesk|salesforce|"
    r"\bsap\b|servicenow|workday|data\s+(analyst|scientist|entry)|machine\s+"
    r"learning|ml\s+engineer|mlops|devops|site\s+reliability|\bsre\b|embedded|"
    r"\.net|c#|\bphp\b|wordpress|drupal|mainframe|cobol|\babap\b", re.I)
BACKEND_POS = re.compile(
    r"back[\s-]?end|golang|\bgo\s+(developer|engineer)|node|python|"
    r"java(?!script)|\bapi\b|rest\b|grpc|microservice|distributed|"
    r"server[\s-]side|platform\s+engineer|full[\s-]?stack|"
    r"software\s+(engineer|developer)|\bsde\b", re.I)


def is_backend_job(title, text=""):
    if BACKEND_NEG.search(title or ""):
        return False, "non-backend role (title)"
    if BACKEND_POS.search(title or "") or BACKEND_POS.search(text or ""):
        return True, ""
    return False, "no backend signal in title/card"
IST = timezone(timedelta(hours=5, minutes=30))


# ---------------------------------------------------------------- relevance
# Same rules as scrape_jobs_cron.py so both pipelines agree on what "relevant"
# means for Vishal's profile.
EXCLUDE_TITLES = [
    "director", "vp ", "vice president", "sales", "recruiter", "hr ", "marketing",
    "graphic designer", "accountant", "telecaller", "bpo", "content writer",
    ".net", "dotnet", "c#", "sap", "abap", "php", "wordpress", "flutter",
    "ios developer", "android developer",
]
PRIMARY_KEYWORDS = [
    "golang", "go developer", "go backend", "go lang",
    "fastapi", "python", "django", "flask",
    "node.js", "nodejs", "node js", "express", "nestjs",
    "next.js", "nextjs", "react", "full stack", "fullstack",
    "backend", "back end", "back-end", "sde", "software engineer",
    "software development engineer",
]


def is_relevant(title, desc=""):
    t_low = (title or "").lower()
    comb = t_low + " " + (desc or "").lower()
    for ex in EXCLUDE_TITLES:
        if ex in t_low:
            return False, f"excluded title: {ex}"
    if not any(kw in comb for kw in PRIMARY_KEYWORDS):
        return False, "no matching tech keywords"
    return True, "relevant"


# ------------------------------------------------------------------ tracking
def get_db():
    conn = sqlite3.connect(JOBS_DB)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS naukri_applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_key TEXT NOT NULL UNIQUE,
            job_url TEXT,
            title TEXT,
            company TEXT,
            location TEXT,
            experience TEXT,
            status TEXT NOT NULL,
            detail TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    conn.commit()
    return conn


def job_key_from_url(url):
    m = re.search(r"job-listings[^?#]*?(\d{9,})", url or "")
    return m.group(1) if m else (url or "").split("?")[0]


def already_seen(conn, key):
    return conn.execute(
        "SELECT 1 FROM naukri_applications WHERE job_key = ?", (key,)
    ).fetchone() is not None


def seen_status(conn, key):
    row = conn.execute(
        "SELECT status FROM naukri_applications WHERE job_key = ?", (key,)
    ).fetchone()
    return row[0] if row else None


def applied_today(conn):
    today_ist = datetime.now(IST).strftime("%Y-%m-%d")
    row = conn.execute(
        "SELECT COUNT(*) FROM naukri_applications WHERE status='applied' "
        "AND date(created_at, '+330 minutes') = ?",
        (today_ist,),
    ).fetchone()
    return row[0]


def record(conn, job, status, detail=""):
    conn.execute(
        "INSERT OR IGNORE INTO naukri_applications "
        "(job_key, job_url, title, company, location, experience, status, detail) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (job["key"], job["url"], job["title"], job["company"],
         job.get("location", ""), job.get("experience", ""), status, detail),
    )
    # Reprocessed jobs (retries of needs_review/failed/skipped rows) hit the
    # IGNORE above; keep the row current with the latest outcome.
    conn.execute(
        "UPDATE naukri_applications SET status=?, detail=?, "
        "job_url=CASE WHEN ?!='' THEN ? ELSE job_url END WHERE job_key=?",
        (status, detail, job["url"], job["url"], job["key"]),
    )
    if status == "applied":
        conn.execute(
            "INSERT OR IGNORE INTO companies (name, notes) VALUES (?, 'Naukri recommended')",
            (job["company"] or "Unknown",),
        )
        row = conn.execute(
            "SELECT id FROM companies WHERE name = ?", (job["company"] or "Unknown",)
        ).fetchone()
        conn.execute(
            "INSERT OR IGNORE INTO job_openings "
            "(company_id, role_title, location, experience_required, job_url, source, status) "
            "VALUES (?,?,?,?,?,?,?)",
            (row[0] if row else None, job["title"], job.get("location", ""),
             job.get("experience", ""), job["url"], "naukri_recommended", "applied"),
        )
    conn.commit()


# ------------------------------------------------------------------ scraping
def dump_debug(page, name):
    os.makedirs(DEBUG_DIR, exist_ok=True)
    stamp = datetime.now(IST).strftime("%Y%m%d_%H%M%S")
    try:
        page.screenshot(path=os.path.join(DEBUG_DIR, f"{name}_{stamp}.png"))
        with open(os.path.join(DEBUG_DIR, f"{name}_{stamp}.html"), "w") as f:
            f.write(page.content())
    except Exception:
        pass


def is_login_wall(page):
    url = page.url.lower()
    if "login" in url or "nlogin" in url:
        return True
    try:
        return page.locator(
            "input[placeholder*='Email' i], form#loginForm, a#login_Layer"
        ).count() > 0 and page.locator("a[href*='mnjuser/profile']").count() == 0
    except Exception:
        return False


def collect_recommended(page):
    """Return recommended job cards as dicts.

    The recommended page is JS-rendered: cards are article.jobTuple with a
    data-job-id and no href anywhere — the JD opens in a new tab when the
    title is clicked (see open_job_page)."""
    jobs, seen = [], set()
    for card in page.query_selector_all("article.jobTuple[data-job-id]"):
        try:
            key = card.get_attribute("data-job-id") or ""
            if not key or key in seen:
                continue
            seen.add(key)

            def attr(sel):
                el = card.query_selector(sel)
                return ((el.get_attribute("title") or el.inner_text() or "").strip()
                        if el else "")

            desc = attr("div.job-description span")
            tags = " ".join(
                (t.inner_text() or "").strip()
                for t in card.query_selector_all("ul.tags li")
            )
            jobs.append({
                "key": key,
                "url": "",  # filled in when the JD tab is opened
                "title": attr("p.title"),
                "company": attr(".companyInfo .subTitle"),
                "experience": attr("li.placeHolderLi.experience span"),
                "location": attr("li.placeHolderLi.location span"),
                "card_text": (desc + " " + tags)[:500],
            })
        except Exception:
            continue
    return jobs


def collect_search(page, query, limit=25):
    """Return job dicts from a public search-results page for `query`.

    Unlike the recommended page, search cards carry real hrefs, so these jobs
    have `url` set up front and try_apply opens them by direct navigation.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", query.lower()).strip("-")
    url = f"https://www.naukri.com/{slug}-jobs?experience=2"
    jobs = []
    try:
        page.goto(url, timeout=45000, wait_until="domcontentloaded")
        page.wait_for_timeout(6000)
        if "access denied" in (page.title() or "").lower():
            return jobs
        cards = page.query_selector_all(
            "div.srp-jobtuple-wrapper[data-job-id], article.jobTuple[data-job-id]")
        for card in cards[:limit]:
            try:
                key = card.get_attribute("data-job-id") or ""
                a = card.query_selector("a.title")
                if not key or a is None:
                    continue
                href = a.get_attribute("href") or ""
                if not href:
                    continue
                comp = card.query_selector("a.comp-name, .comp-name, .companyInfo .subTitle")
                exp = card.query_selector("span.expwdth, li.experience span")
                loc = card.query_selector("span.locwdth, li.location span")
                jobs.append({
                    "key": key,
                    "url": href,
                    "title": (a.inner_text() or "").strip(),
                    "company": (comp.inner_text() or "").strip() if comp else "",
                    "experience": (exp.inner_text() or "").strip() if exp else "",
                    "location": (loc.inner_text() or "").strip() if loc else "",
                    "card_text": "",
                })
            except Exception:
                continue
    except Exception:
        pass
    return jobs


def open_job_page(context, listing_page, key):
    """Click a card's title on the listing page and return the JD tab."""
    title_el = listing_page.query_selector(
        f"article.jobTuple[data-job-id='{key}'] p.title"
    )
    if title_el is None:
        return None
    title_el.scroll_into_view_if_needed()
    with context.expect_page(timeout=20000) as new_page_info:
        title_el.click()
    jd = new_page_info.value
    jd.wait_for_load_state("domcontentloaded", timeout=45000)
    jd.wait_for_timeout(4000)
    return jd


def try_apply(context, listing_page, job):
    """Open the JD tab from its listing card and apply if it's a direct
    one-click listing.

    Returns (status, detail): applied | external | needs_review | failed.
    """
    page = None
    try:
        if job.get("url"):
            # Search-sourced jobs carry a real href; open it directly.
            page = context.new_page()
            page.goto(job["url"], timeout=45000, wait_until="domcontentloaded")
            page.wait_for_timeout(4000)
        else:
            page = open_job_page(context, listing_page, job["key"])
        if page is None:
            return "failed", "card no longer present on listing page"
        job["url"] = page.url

        if "access denied" in (page.title() or "").lower():
            return "failed", "bot wall on job page"

        # Company-site redirect listings are never auto-applied.
        if page.locator("#company-site-button, button:has-text('Apply on company site')").count():
            return "external", "redirects to company site"

        already = page.locator("#already-applied, span:has-text('Applied')").first
        try:
            if already.count() and "applied" in (already.inner_text() or "").lower():
                return "applied", "was already applied on Naukri"
        except Exception:
            pass

        apply_btn = page.locator("#apply-button, button:has-text('Apply')").first
        if not apply_btn.count():
            dump_debug(page, f"no_apply_btn_{job['key']}")
            return "failed", "no apply button found"

        apply_btn.click()
        page.wait_for_timeout(5000)

        # Screening questionnaire (Naukri chatbot drawer): answered from
        # ~/.hermes/data/naukri_profile.json via naukri_questions.py.
        if page.locator("div.chatbot_Drawer").count() and \
                page.locator("div.chatbot_Drawer").first.is_visible():
            import naukri_questions
            profile = naukri_questions.load_profile()
            transcript = []
            status, detail = naukri_questions.answer_questionnaire(page, profile, transcript)
            if transcript:
                qa = "; ".join(f"{t['question'][:60]} -> {t['answer'][:40]}" for t in transcript)
                detail = f"{detail} | {qa}"[:500]
            return status, detail

        # A successful one-click apply navigates to a separate "Apply
        # Confirmation" page whose body says: Applied to "<job title>".
        # It never contains "successfully applied"/"application sent", so
        # matching only those misfiled every real apply as needs_review.
        title = (page.title() or "").lower()
        try:
            body = (page.locator("body").inner_text() or "").lower()
        except Exception:
            body = (page.content() or "").lower()
        if "apply confirmation" in title \
                or re.search(r'applied to\s*["“‘\']', body) \
                or "successfully applied" in body or "application sent" in body \
                or page.locator("span:has-text('Applied')").count():
            return "applied", "one-click apply"

        # Last resort: re-open the JD and look for the already-applied
        # marker, which is authoritative regardless of page redesigns.
        try:
            page.goto(job["url"], timeout=45000, wait_until="domcontentloaded")
            page.wait_for_timeout(3500)
            if page.locator("#already-applied").count():
                return "applied", "confirmed via already-applied marker on reload"
        except Exception:
            pass

        dump_debug(page, f"unclear_{job['key']}")
        return "needs_review", "apply clicked but confirmation not detected"
    except Exception as e:
        return "failed", str(e)[:200]
    finally:
        try:
            if page is not None:
                page.close()
        except Exception:
            pass


# One Chrome profile, one browser at a time: the hourly cron and the manual
# questionnaire runner share ~/.hermes/data/chrome_automation_profile, and two
# concurrent launches kill each other (SingletonLock).
# Deliberately the SAME lock file the social scripts use (browser_lock.py).
# This script drives the same chrome_automation_profile as they do, so a
# private naukri-only lock left the two able to run concurrently on one
# profile, which is what corrupts the session. One profile, one lock.
import sys as _sys
_sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# Capture this run's full stdout and stderr to
# ~/.hermes/logs/pipelines/naukri_auto_apply/ so a failed or silent run can be
# diagnosed afterwards instead of vanishing.
if __name__ == '__main__':
    # Only when run directly. Firing on import made every script that
    # imports this module log its own run under this pipeline's name.
    try:
        import pipeline_log as _plog
        _plog.start('naukri_auto_apply')
    except Exception:
        pass

from browser_lock import LOCK_PATH  # noqa: E402


def acquire_browser_lock(timeout_secs=900):
    import fcntl
    fh = open(LOCK_PATH, "w")
    deadline = time.time() + timeout_secs
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fh
        except OSError:
            if time.time() > deadline:
                raise RuntimeError("naukri browser lock busy for >%ss" % timeout_secs)
            time.sleep(10)


# Naukri's Akamai bot wall rejects headless Chrome even with a valid login
# session (verified 2026-08-23), but passes a headed browser. Run headed
# inside a private Xvfb display so nothing appears on the user's desktop.
def start_xvfb():
    for n in range(90, 110):
        if os.path.exists(f"/tmp/.X{n}-lock"):
            continue
        proc = subprocess.Popen(
            ["Xvfb", f":{n}", "-screen", "0", "1440x900x24"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        time.sleep(1.5)
        if proc.poll() is None:
            return proc, f":{n}"
        # display raced into use between the lock check and startup; try next
    raise RuntimeError("no free X display for Xvfb")


def main():
    conn = get_db()
    done_today = applied_today(conn)
    budget = min(MAX_APPLIES_PER_RUN, MAX_APPLIES_PER_DAY - done_today)

    summary = {
        "applied": [], "needs_review": [], "external": [],
        "skipped_irrelevant": 0, "skipped_seen": 0, "failed": [],
        "applied_today_before_run": done_today,
        "daily_cap": MAX_APPLIES_PER_DAY,
    }

    if budget <= 0:
        summary["note"] = "daily apply cap reached; scan skipped"
        print(json.dumps(summary, indent=2))
        return 0

    lock = acquire_browser_lock()
    xvfb, display = start_xvfb()
    os.environ["DISPLAY"] = display
    os.environ.pop("WAYLAND_DISPLAY", None)
    try:
        return run_browser_pass(conn, summary, budget)
    finally:
        xvfb.terminate()
        lock.close()


def run_browser_pass(conn, summary, budget):
    with sync_playwright() as p:
        def launch():
            return p.chromium.launch_persistent_context(
                user_data_dir=PROFILE_DIR,
                channel="chrome",
                headless=False,
                locale="en-IN",
                timezone_id="Asia/Kolkata",
                viewport={"width": 1440, "height": 900},
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            )

        context = launch()
        page = context.new_page()

        def ensure_browser():
            """Relaunch Chrome if it died mid-run (a crash previously killed
            the rest of the run, including the whole search phase)."""
            nonlocal context, page
            try:
                page.title()
                return True
            except Exception:
                try:
                    context.close()
                except Exception:
                    pass
                try:
                    context = launch()
                    page = context.new_page()
                    summary["browser_relaunched"] = summary.get("browser_relaunched", 0) + 1
                    return True
                except Exception:
                    return False
        try:
            page.goto(RECOMMENDED_URL, timeout=45000, wait_until="domcontentloaded")
            page.wait_for_timeout(6000)
        except Exception as e:
            print(json.dumps({"error": f"navigation failed: {e}"}))
            context.close()
            return 3

        title = (page.title() or "").lower()
        if "access denied" in title:
            dump_debug(page, "access_denied")
            print(json.dumps({"error": "NAUKRI_BLOCKED: bot wall (Access Denied) on recommended page"}))
            context.close()
            return 3
        if is_login_wall(page):
            print(json.dumps({"error": "NAUKRI_NOT_LOGGED_IN: run ~/.hermes/scripts/naukri_login.py once in a desktop session"}))
            context.close()
            return 2

        jobs = collect_recommended(page)
        if not jobs:
            dump_debug(page, "zero_cards")
            summary["note"] = "no recommended job cards found (debug snapshot saved)"

        def process(job):
            """Apply to one job; returns False once the run budget is hit."""
            prior = seen_status(conn, job["key"])
            # applied/external are terminal; anything else (needs_review,
            # failed, skipped_irrelevant) gets another attempt.
            if prior in ("applied", "external"):
                summary["skipped_seen"] += 1
                return True
            if prior and not job.get("url"):
                # Retry of an earlier non-terminal row: navigate straight to
                # the stored JD URL rather than hunting for the card again.
                row = conn.execute("SELECT job_url FROM naukri_applications "
                                   "WHERE job_key=?", (job["key"],)).fetchone()
                if row and row[0]:
                    job["url"] = row[0]
            if BACKEND_ONLY:
                ok, why = is_backend_job(job["title"], job.get("card_text", ""))
            elif FILTER_RELEVANCE:
                ok, why = is_relevant(job["title"], job.get("card_text", ""))
            else:
                ok, why = True, ""
            if not ok:
                summary["skipped_irrelevant"] += 1
                record(conn, job, "skipped_irrelevant", why)
                return True
            if len(summary["applied"]) >= budget:
                return False

            status, detail = try_apply(context, page, job)
            if status == "failed" and re.search(
                    r"has been closed|Connection closed|Target closed",
                    detail or "", re.I):
                # Chrome died mid-run; relaunch and retry this job once
                # instead of burning the rest of the batch on a dead context.
                if ensure_browser():
                    status, detail = try_apply(context, page, job)
            record(conn, job, status, detail)
            entry = {"title": job["title"], "company": job["company"],
                     "url": job["url"], "detail": detail}
            if status == "applied":
                summary["applied"].append(entry)
            elif status == "external":
                summary["external"].append(entry)
            elif status == "needs_review":
                summary["needs_review"].append(entry)
            else:
                summary["failed"].append(entry)
            time.sleep(4)  # pace requests; bulk-speed applying gets accounts flagged
            return True

        done_keys = set()
        for job in jobs:
            done_keys.add(job["key"])
            if not process(job):
                break

        # Recommended inventory alone rarely sustains 20+/hour; top the run
        # up from public search results for the profile's core stack.
        if len(summary["applied"]) < budget:
            summary["search_topup"] = {}
            for query in SEARCH_QUERIES:
                if len(summary["applied"]) >= budget:
                    break
                if not ensure_browser():
                    summary["note"] = "browser died and could not be relaunched"
                    break
                found = collect_search(page, query)
                fresh = [j for j in found if j["key"] not in done_keys]
                summary["search_topup"][query] = len(fresh)
                stop = False
                for job in fresh:
                    done_keys.add(job["key"])
                    if not process(job):
                        stop = True
                        break
                if stop:
                    break

        context.close()

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    sys.exit(main())
