"""Instahyre auto-apply — rebuilt 2026-09-12 on the wellfound/foundit architecture.

The previous version failed every scheduled run for two weeks. Root cause was one
line: it pointed launch_persistent_context at ~/.config/google-chrome, the user's
real daily-driver profile, which Chrome holds open — so Playwright could never
acquire it. It also had no profile_lock, no Xvfb, and no database, meaning it could
re-apply to the same job every day and kept no record of what it sent.

Flow, verified against live pages 2026-09-12:
  - The shared automation profile is ALREADY signed in; no login step is needed
    (INSTAHYRE_EMAIL/PASSWORD remain in .env as a fallback).
  - /candidate/opportunities/?matching= lists recommended jobs, 22 at the time of
    writing, each with a "View »" button (id=interested-btn) and "Not interested".
  - "View »" (id=interested-btn, class=button-interested) is NOT neutral. It opens
    the detail modal AND marks the job "Interested" on the account, removing it
    from the matching queue. Verified 2026-09-12: the status filter read
    "Undecided (0) | Interested (128) | Not Interested (0)" and the queue was
    empty — the old script had clicked View on every keyword match for weeks.
    So a job must be FILTERED BEFORE it is opened. There is no backing out.
  - Consequence: screening runs on the list card only. The card gives company,
    title, location and a description snippet — but NOT the experience band,
    which lives in the modal and is therefore unavailable at decision time.
  - The modal's blue "Apply" button is the actual application.
  - We never click "Not interested": it permanently dismisses a job. Rejected jobs
    are simply left alone, recorded in the DB so they are not reconsidered.

Instahyre exposes no job id, so the dedupe key is a slug of company + title.
"""
import json, os, re, sqlite3, subprocess, sys, time, urllib.request
from playwright.sync_api import sync_playwright
from browser_lock import profile_lock

PROFILE_DIR = os.path.expanduser("~/.hermes/data/chrome_automation_profile")
JOBS_DB = os.path.expanduser("~/.hermes/data/jobs_tracker.db")
HERMES_ENV = os.path.expanduser("~/.hermes/.env")
OPPS_URL = "https://www.instahyre.com/candidate/opportunities/?matching="
MAX_APPLIES_PER_RUN = int(os.environ.get("INSTAHYRE_MAX_PER_RUN", 20))
TIME_BUDGET = int(os.environ.get("INSTAHYRE_TIME_BUDGET", 420))
MAX_MIN_YEARS = int(os.environ.get("INSTAHYRE_MAX_MIN_YEARS", 4))
API_URL = os.environ.get(
    "SCRIPT_LLM_API_URL",
    "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions")
MODEL = os.environ.get("INSTAHYRE_AI_MODEL", "gemini-3.5-flash-lite")
DRY_RUN = "--dry-run" in sys.argv

CANDIDATE_FACTS = """Vishal Panwar — Backend Engineer, 2+ years, based in Hyderabad, India.
Stack: Go, Node.js, Python, TypeScript; PostgreSQL, Redis, MongoDB; Kubernetes, Docker, GCP, AWS, Terraform; NATS, gRPC, microservices.
Wins: cut API p95 latency 40% at 1K+ RPS (Go buffer pooling); reduced PostgreSQL load 60% (Redis caching + distributed locking); NATS event-driven microservices with KEDA autoscaling; at Cognizant built REST APIs for 1,000+ users on Azure, promoted in 6 months.
Work situation: based in Hyderabad, works fully remote; NOT willing to relocate to another Indian city for an onsite role; remote or Hyderabad only; can start immediately."""


def load_env_key(name="SCRIPT_LLM_API_KEY"):
    val = os.environ.get(name)
    if val:
        return val.strip()
    try:
        with open(HERMES_ENV) as fh:
            for line in fh:
                if line.strip().startswith(f"{name}="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return None


API_KEY = load_env_key()


def ai_json(prompt, max_tokens=400):
    if not API_KEY:
        return None
    body = json.dumps({"model": MODEL,
                       "messages": [{"role": "user", "content": prompt}],
                       "max_tokens": max_tokens}).encode()
    req = urllib.request.Request(
        API_URL, data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {API_KEY}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            txt = json.loads(r.read())["choices"][0]["message"]["content"]
        m = re.search(r"\{.*\}", txt, re.S)
        return json.loads(m.group(0)) if m else None
    except Exception:
        return None


def init_db():
    conn = sqlite3.connect(JOBS_DB)
    conn.execute("""CREATE TABLE IF NOT EXISTS instahyre_applications (
        id INTEGER PRIMARY KEY AUTOINCREMENT, job_key TEXT UNIQUE, job_url TEXT,
        title TEXT, company TEXT, status TEXT, detail TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    conn.commit()
    conn.close()


def seen_before(conn, key):
    return conn.execute(
        "SELECT id FROM instahyre_applications WHERE job_key=? AND status IN "
        "('applied','external','filtered')", (key,)).fetchone() is not None


def record(conn, key, url, title, company, status, detail):
    conn.execute("INSERT OR REPLACE INTO instahyre_applications "
                 "(job_key,job_url,title,company,status,detail) VALUES (?,?,?,?,?,?)",
                 (key, url, title, company, status, detail))
    conn.commit()


def start_xvfb():
    for n in range(90, 110):
        if os.path.exists(f"/tmp/.X{n}-lock"):
            continue
        proc = subprocess.Popen(["Xvfb", f":{n}", "-screen", "0", "1440x900x24"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.5)
        if proc.poll() is None:
            return proc, f":{n}"
    raise RuntimeError("no free X display")


MODAL_JS = """() => {
    const m = document.querySelector('.modal-content, [class*="modal" i]');
    if (!m) return null;
    const t = (m.innerText || '');
    return {text: t.slice(0, 4000), len: t.length};
}"""

EXP_RE = re.compile(r"(\d+)\s*-\s*(\d+)\s*years", re.I)


HOME_CITIES = ("remote", "hyderabad", "anywhere", "work from home")


def parse_card(text):
    """List-card text, e.g.
        Purplle - Software Development Engineer
        Job available in Pune
        Founded in 2012 - 200 - 500 employees
        <description snippet>
    """
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    out = {"company": "?", "title": "", "location": "", "snippet": ""}
    if lines:
        head = lines[0]
        if " - " in head:
            out["company"], out["title"] = [p.strip() for p in head.split(" - ", 1)]
        else:
            out["title"] = head
    for l in lines[1:5]:
        m = re.search(r"job available in\s+(.+)", l, re.I)
        if m:
            out["location"] = m.group(1).strip()
            break
    out["snippet"] = " ".join(lines[1:])[:800]
    return out


def location_filter(job):
    """Hyderabad or remote only — he will not relocate within India.

    The experience band is NOT available here: it lives in the modal, which cannot
    be opened without committing to Interested. So experience is left to the LLM,
    which sees the description snippet.
    """
    loc = (job.get("location") or "").lower()
    if not loc:
        return True, "no location on card"
    if any(c in loc for c in HOME_CITIES):
        return True, f"location ok ({job['location']})"
    return False, f"onsite in {job['location']}, candidate is Hyderabad/remote only"


def parse_modal(text):
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    out = {"title": "", "company": "", "location": "", "exp_min": None,
           "exp_max": None}
    # The modal opens: <title> / <company> / <location> / <experience band>
    for i, l in enumerate(lines[:14]):
        if not out["title"] and len(l) > 3 and "at a glance" not in l.lower():
            out["title"] = l
            if i + 1 < len(lines):
                out["company"] = lines[i + 1]
            continue
    m = EXP_RE.search(text)
    if m:
        out["exp_min"], out["exp_max"] = int(m.group(1)), int(m.group(2))
    for l in lines[:14]:
        if re.search(r"\b(remote|hyderabad|bengaluru|bangalore|pune|mumbai|delhi|"
                     r"noida|gurgaon|chennai|kolkata|jaipur|ahmedabad)\b", l, re.I) \
                and len(l) < 60 and not EXP_RE.search(l):
            out["location"] = l
            break
    return out


def job_key(company, title):
    return re.sub(r"[^a-z0-9]+", "_", f"{company}_{title}".lower())[:60]


def experience_filter(job):
    if job["exp_min"] is None:
        return True, "no experience band stated"
    if job["exp_min"] > MAX_MIN_YEARS:
        return False, f"needs {job['exp_min']}-{job['exp_max']} yrs, candidate has ~2"
    return True, f"experience band {job['exp_min']}-{job['exp_max']} ok"


def ai_verdict(job, desc):
    v = ai_json(f"""You screen jobs for this candidate:
{CANDIDATE_FACTS}

Job: {job['title']} at {job['company']}
Location: {job['location']} | Experience asked: {job['exp_min']}-{job['exp_max']} years
Description (truncated): {desc[:1500]}

Return JSON only: {{"apply": true/false, "reason": "one line"}}

JUDGE ON THE TECH, NOT THE JOB TITLE. SRE, DevOps, Platform, Infrastructure, Cloud,
Systems, Production, Observability, Backend, Full Stack and plain Software Engineer
are all acceptable titles. apply=true when the work is any of:
  - reliability / SRE: uptime, on-call, incident response, SLOs, scaling
  - devops / platform / infrastructure: Kubernetes, Docker, Terraform, CI/CD, AWS/GCP
  - observability: metrics, tracing, logging, Prometheus, Grafana, OpenTelemetry
  - backend: APIs, services, Go/Node/Python, PostgreSQL/Redis/Mongo, gRPC, queues
  - full stack: backend as above with a TypeScript/React front end

apply=false if it needs 5+ more years than he has, is a different discipline
(mobile, embedded, frontend-only, data science / ML research, security-only,
QA-only, support, sales, management), or requires being onsite in an Indian city
other than Hyderabad. Remote anywhere in India is fine — if the description says
remote, prefer that over the city on the listing.""")
    if not v or "apply" not in v:
        return {"apply": True, "reason": "ai unavailable, rule-pass"}
    return v


def close_modal(page):
    for sel in ['.modal-content button.close', '.modal .close',
                'button[aria-label="Close"]', '.modal-content .close']:
        try:
            el = page.locator(sel).first
            if el.count() and el.is_visible(timeout=1500):
                el.click()
                time.sleep(1.5)
                return
        except Exception:
            pass
    try:
        page.keyboard.press("Escape")
        time.sleep(1.5)
    except Exception:
        pass


def main():
    init_db()
    conn = sqlite3.connect(JOBS_DB)
    deadline = time.time() + TIME_BUDGET
    done = conn.execute(
        "SELECT COUNT(*) FROM instahyre_applications WHERE status='applied' "
        "AND date(created_at,'+330 minutes')=date('now','+330 minutes')").fetchone()[0]
    budget = min(MAX_APPLIES_PER_RUN, 100 - done)
    summary = {"applied": [], "needs_review": [], "external": [], "failed": [],
               "filtered": [], "skipped_seen": 0, "applied_today_before_run": done,
               "dry_run": DRY_RUN}
    if budget <= 0:
        summary["note"] = "daily cap"
        print(json.dumps(summary, indent=2))
        return 0

    try:
        with profile_lock('instahyre', timeout=600):
            deadline = time.time() + TIME_BUDGET  # budget starts once the profile is ours
            xvfb, disp = start_xvfb()
            os.environ["DISPLAY"] = disp
            os.environ.pop("WAYLAND_DISPLAY", None)
            try:
                with sync_playwright() as p:
                    ctx = p.chromium.launch_persistent_context(
                        user_data_dir=PROFILE_DIR, channel="chrome", headless=False,
                        locale="en-IN", timezone_id="Asia/Kolkata",
                        viewport={"width": 1440, "height": 900},
                        args=["--disable-blink-features=AutomationControlled",
                              "--no-sandbox"])
                    page = ctx.new_page()
                    page.goto(OPPS_URL, timeout=45000, wait_until="domcontentloaded")
                    time.sleep(7)
                    if "login" in page.url.lower():
                        summary["error"] = "INSTAHYRE_NOT_LOGGED_IN"
                        print(json.dumps(summary, indent=2))
                        ctx.close()
                        return 2

                    cards = page.evaluate("""() => Array.from(
                        document.querySelectorAll('a.row.text-link'))
                        .filter(e => e.offsetParent !== null)
                        .map(e => (e.innerText || '').slice(0, 600))""")
                    total = len(cards)
                    print(f"[ih] {total} opportunities on the list", file=sys.stderr)
                    if not total:
                        summary["note"] = "no matching opportunities in the queue"

                    # Applying removes a card from the queue, so positional indices
                    # shift underneath us. Re-read the list each pass and take the
                    # first card we have not already decided on.
                    handled = set()
                    for _ in range(total):
                        if len(summary["applied"]) >= budget:
                            break
                        if time.time() > deadline:
                            summary["note"] = "stopped at time budget"
                            break
                        cards = page.evaluate("""() => Array.from(
                            document.querySelectorAll('a.row.text-link'))
                            .filter(e => e.offsetParent !== null)
                            .map(e => (e.innerText || '').slice(0, 600))""")
                        idx = job = key = None
                        for i, text in enumerate(cards):
                            j = parse_card(text)
                            k = job_key(j["company"], j["title"])
                            if k in handled:
                                continue
                            idx, job, key, card_text = i, j, k, text
                            break
                        if job is None:
                            break
                        handled.add(key)

                        try:
                            if seen_before(conn, key):
                                summary["skipped_seen"] += 1
                                continue

                            # Screen BEFORE opening: clicking View marks Interested.
                            ok, why = location_filter(job)
                            if ok:
                                v = ai_verdict(job, card_text)
                                if not v.get("apply", True):
                                    ok, why = False, "ai: " + str(v.get("reason", ""))[:140]

                            if not ok:
                                # No click at all — the job stays Undecided.
                                st, det = "filtered", why
                            elif DRY_RUN:
                                st, det = "filtered", "dry-run: would have opened and applied"
                            else:
                                page.locator('button:has-text("View")').nth(idx).click()
                                time.sleep(4)
                                ap = page.locator(
                                    '.modal-content button:has-text("Apply"), '
                                    '.modal button:has-text("Apply")').first
                                if not ap.count():
                                    st, det = "needs_review", "opened (now Interested) but no Apply button"
                                else:
                                    ap.click()
                                    time.sleep(4)
                                    st, det = "applied", "apply clicked"
                                close_modal(page)
                                time.sleep(2)

                            if not DRY_RUN:
                                record(conn, key, OPPS_URL, job["title"],
                                       job["company"], st, det)
                            summary.setdefault(st, summary["failed"]).append(
                                {"title": job["title"], "company": job["company"],
                                 "location": job["location"], "detail": det})
                            time.sleep(1)
                        except Exception as e:
                            print(f"[ih] {key}: {str(e)[:120]}", file=sys.stderr)
                            close_modal(page)
                    ctx.close()
            finally:
                xvfb.terminate()
    except Exception as e:
        summary["error"] = str(e)

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
