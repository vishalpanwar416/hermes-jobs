"""Foundit (foundit.in) auto-apply — AI-gated, tech-first.

Same architecture as wellfound_auto_apply.py (profile_lock + Xvfb + sqlite dedupe
+ cheap rule filter + LLM fit gate), with three differences that Foundit forces:

  1. Login is Google SSO. The shared automation profile already carries a Google
     session, so login is "click the Google button and wait" — there are no
     FOUNDIT_* credentials anywhere and none are needed.
  2. Job cards are NOT anchors. Each ``div.srpResultCardContainer`` holds an inner
     ``div`` whose id IS the numeric job id, and clicking ``#jobCardTitle`` renders
     the detail in-page rather than navigating. So there is no per-job URL to
     collect; the id is the dedupe key and the "url" recorded is the search page.
  3. The cheap filter is EXPERIENCE, not visa. This is an India board, so almost
     nothing here needs sponsorship, but listings state a hard experience band
     ("5 - 8 Years") that is free to parse off the card and is what actually
     disqualifies a 2-year candidate. Location goes to the LLM instead of a rule,
     because "Bengaluru, India" on the card often still means remote in the body.

Verified against live pages 2026-09-10: search URL differentiates by query
(devops 1436 / SRE 428 / backend 622 / observability 1732 / full stack 1104),
card ids resolve, and the detail view exposes "Apply Now". The Apply click itself
is the one path not yet exercised end to end — run with --dry-run first.
"""
import json, os, re, sqlite3, subprocess, sys, time, urllib.request
from playwright.sync_api import sync_playwright
from browser_lock import profile_lock, headless_launch_kwargs, virtual_display

PROFILE_DIR = os.path.expanduser("~/.hermes/data/chrome_automation_profile")
JOBS_DB = os.path.expanduser("~/.hermes/data/jobs_tracker.db")
HERMES_ENV = os.path.expanduser("~/.hermes/.env")
MAX_APPLIES_PER_RUN = int(os.environ.get("FOUNDIT_MAX_PER_RUN", 20))
TIME_BUDGET = int(os.environ.get("FOUNDIT_TIME_BUDGET", 900))
BASE = "https://www.foundit.in"
API_URL = os.environ.get(
    "SCRIPT_LLM_API_URL",
    "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions")
MODEL = os.environ.get("FOUNDIT_AI_MODEL", "gemini-3.5-flash-lite")

DRY_RUN = "--dry-run" in sys.argv

# Candidate has 2+ years. A band starting above this is an automatic no, and it is
# the single cheapest signal on the page — it comes off the card without opening
# the job at all.
MAX_MIN_YEARS = int(os.environ.get("FOUNDIT_MAX_MIN_YEARS", 4))

# Tech domains, not job titles — the same net as wellfound's SEARCH_ROLES.
SEARCH_QUERIES = [
    "site reliability engineer", "devops engineer", "observability",
    "platform engineer", "backend engineer", "golang", "full stack engineer",
]
LOCATIONS = os.environ.get("FOUNDIT_LOCATIONS", "India")
# Page 1 of each query (15 cards) was all already seen by 2026-09-25, so runs
# found 0 new jobs. Results are relevance-ordered and page with &start=N; a
# 09-27 probe of "backend engineer" found 0,0,1,8,12 unseen on pages 1-5.
SEARCH_PAGES = int(os.environ.get("FOUNDIT_SEARCH_PAGES", 5))

CANDIDATE_FACTS = """Vishal Panwar — Backend Engineer, 2+ years, based in Hyderabad, India.
Stack: Go, Node.js, Python, TypeScript; PostgreSQL, Redis, MongoDB; Kubernetes, Docker, GCP, AWS, Terraform; NATS, gRPC, microservices.
Wins: cut API p95 latency 40% at 1K+ RPS (Go buffer pooling); reduced PostgreSQL load 60% (Redis caching + distributed locking); NATS event-driven microservices with KEDA autoscaling; at Cognizant built REST APIs for 1,000+ users on Azure, promoted in 6 months.
Projects: Table-Tap (live contactless QR ordering, GPT-4 RAG analytics, 500+ concurrent orders, GCP Cloud Run) https://table-tap.in ; multi-qr npm package https://www.npmjs.com/package/multi-qr
Links: LinkedIn https://linkedin.com/in/vishalpanwar416 | GitHub https://github.com/vishalpanwar416 | Site https://vishalpanwar.in
Work situation: based in Hyderabad, works fully remote; NOT willing to relocate to another Indian city for an onsite role; remote or Hyderabad only; can start immediately."""


def load_env_key(name="SCRIPT_LLM_API_KEY"):
    val = os.environ.get(name)
    if val:
        return val.strip()
    try:
        with open(HERMES_ENV) as fh:
            for line in fh:
                line = line.strip()
                if line.startswith(f"{name}="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return None


API_KEY = load_env_key()


def ai_json(prompt, max_tokens=700):
    if not API_KEY:
        return None
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
    }).encode()
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
    conn.execute("""CREATE TABLE IF NOT EXISTS foundit_applications (
        id INTEGER PRIMARY KEY AUTOINCREMENT, job_key TEXT UNIQUE, job_url TEXT,
        title TEXT, company TEXT, status TEXT, detail TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    conn.commit()
    conn.close()


def seen_before(conn, key):
    return conn.execute(
        "SELECT id FROM foundit_applications WHERE job_key=? AND status IN "
        "('applied','external','filtered')", (key,)).fetchone() is not None


def record(conn, key, url, title, company, status, detail):
    conn.execute(
        "INSERT OR REPLACE INTO foundit_applications "
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


def login_foundit(page):
    """Google SSO. The automation profile carries the Google session already."""
    try:
        page.goto(f"{BASE}/seeker/dashboard", timeout=45000,
                  wait_until="domcontentloaded")
        time.sleep(5)
        if "login" not in page.url.lower():
            print("[fi] Already logged in", file=sys.stderr)
            return True
        btn = page.locator('button:has-text("Google")').first
        if not btn.count():
            print("[fi] No Google button on login page", file=sys.stderr)
            return False
        btn.click()
        time.sleep(10)
        ok = "login" not in page.url.lower()
        print(f"[fi] Google SSO {'OK' if ok else 'FAILED'} -> {page.url[:70]}",
              file=sys.stderr)
        return ok
    except Exception as e:
        print(f"[fi] Login error: {e}", file=sys.stderr)
        return False


EXP_RE = re.compile(r"(\d+)\s*-\s*(\d+)\s*Years", re.I)


def parse_card(text, title=None):
    """Pull title/company/experience/location out of a card's own innerText.

    Foundit's detail pages do not carry clean 'Location:' / 'Experience:' labels —
    grabbing by label picks up prose out of the JD instead (verified). The card
    text is the reliable source, and it is free.
    """
    # Drop the dismissable onboarding overlay ("Use checkbox to multi select…" /
    # "Got it!"). It renders inside a card's innerText, and a run that hit it
    # recorded the tooltip as the job title with "Got it!" as the company.
    CHROME = ("use checkbox", "got it", "apply to multiple jobs", "hi vishal")
    parts = [p.strip() for p in text.split("\n")
             if p.strip() and not any(c in p.lower() for c in CHROME)]
    out = {"title": title or (parts[0] if parts else ""),
           "company": "?", "exp_min": None, "exp_max": None, "location": ""}
    # Company is the line after the title, whichever line the title turned out to be.
    for i, p in enumerate(parts):
        if p == out["title"] and i + 1 < len(parts):
            out["company"] = parts[i + 1]
            break
    else:
        if len(parts) > 1:
            out["company"] = parts[1]
    m = EXP_RE.search(text)
    if m:
        out["exp_min"], out["exp_max"] = int(m.group(1)), int(m.group(2))
    for p in parts:
        if "," in p and re.search(r"india|remote", p, re.I) and not EXP_RE.search(p):
            out["location"] = p
            break
    return out


def experience_filter(card):
    """Cheap, numeric, and the main disqualifier on this board."""
    if card["exp_min"] is None:
        return True, "no experience band stated"
    if card["exp_min"] > MAX_MIN_YEARS:
        return False, f"needs {card['exp_min']}-{card['exp_max']} yrs, candidate has ~2"
    return True, f"experience band {card['exp_min']}-{card['exp_max']} ok"


def ai_verdict(card, desc):
    prompt = f"""You screen jobs for this candidate:
{CANDIDATE_FACTS}

Job: {card['title']} at {card['company']}
Stated experience: {card['exp_min']}-{card['exp_max']} years | Location on card: {card['location']}
Description (truncated): {desc[:1200]}

Return JSON only:
{{"apply": true/false, "reason": "one line"}}

JUDGE ON THE TECH, NOT THE JOB TITLE. Titles for identical work vary a lot —
Site Reliability, DevOps, Platform, Infrastructure, Cloud, Systems, Production,
Observability, Backend, Full Stack and plain Software Engineer are all acceptable.
Set apply=true when the day-to-day work is any of:
  - reliability / SRE: uptime, on-call, incident response, SLOs, scaling
  - devops / platform / infrastructure: Kubernetes, Docker, Terraform, CI/CD, AWS/GCP
  - observability: metrics, tracing, logging, Prometheus, Grafana, OpenTelemetry
  - backend: APIs, services, Go/Node/Python, PostgreSQL/Redis/Mongo, gRPC, queues
  - full stack: backend work as above with a TypeScript/React front end
A title outside that list is fine if the described stack is in it, and a matching
title is NOT enough if the described work is something else.

Rules: apply=false if the role needs 5+ more years than he has, is a genuinely
different discipline (mobile, embedded, frontend-only, data science / ML research,
security-only, QA-only, support, sales, management), or requires being onsite in an
Indian city other than Hyderabad. Remote anywhere in India is fine. If the location
looks like another city but the description says remote, that is fine — prefer the
description."""
    v = ai_json(prompt)
    if not v or "apply" not in v:
        return {"apply": True, "reason": "ai unavailable, rule-pass"}
    return v


def open_and_apply(page, card_el, card, dry_run):
    """Open the in-page detail for a card, gate it, then click Apply Now."""
    title_el = card_el.locator("#jobCardTitle").first
    if not title_el.count():
        return "failed", "no jobCardTitle in card"
    title_el.click()
    time.sleep(5)

    # Scope to the detail pane. Reading document.body here fed the model the whole
    # results rail alongside the job, and it answered accordingly — verdicts came
    # back as "no matching jobs in the list", judging a list instead of one role.
    desc = ""
    for sel in ['[class*="jdContainer" i]', '[class*="jobDescription" i]']:
        el = page.locator(sel).first
        try:
            if el.count():
                desc = el.inner_text()
                if len(desc) > 300:
                    break
        except Exception:
            pass
    if len(desc) < 300:
        desc = page.locator("body").inner_text()
    desc = desc[:4000]
    v = ai_verdict(card, desc)
    if not v.get("apply", True):
        return "filtered", "ai: " + str(v.get("reason", ""))[:150]

    # "Quick Apply" is a FILTER control on the results rail (class quickApplyFilter),
    # not an apply action — matching it would click the wrong thing entirely.
    btn = None
    for sel in ['button:has-text("Apply Now")', 'button:has-text("Apply")']:
        for i in range(page.locator(sel).count()):
            b = page.locator(sel).nth(i)
            try:
                cls = (b.get_attribute("class") or "")
                if "quickApplyFilter" in cls:
                    continue
                if b.is_visible(timeout=2000) and b.is_enabled():
                    btn = b
                    break
            except Exception:
                pass
        if btn:
            break
    if not btn:
        return "needs_review", "no Apply Now button (already applied, or external)"

    if dry_run:
        return "filtered", "dry-run: would have applied"

    btn.click()
    time.sleep(5)
    body = page.locator("body").inner_text().lower()
    if "applied" in body or "application sent" in body or "successfully" in body:
        return "applied", "apply confirmed"
    return "needs_review", "clicked apply, no confirmation text"


def main():
    init_db()
    conn = sqlite3.connect(JOBS_DB)
    deadline = time.time() + TIME_BUDGET
    done = conn.execute(
        "SELECT COUNT(*) FROM foundit_applications WHERE status='applied' "
        "AND date(created_at,'+330 minutes')=date('now','+330 minutes')").fetchone()[0]
    budget = min(MAX_APPLIES_PER_RUN, 100 - done)
    summary = {"applied": [], "needs_review": [], "external": [], "failed": [],
               "filtered": [], "skipped_seen": 0, "applied_today_before_run": done,
               "dry_run": DRY_RUN}
    if budget <= 0:
        summary["note"] = "daily cap"
        print(json.dumps(summary, indent=2))
        return 0
    if not API_KEY:
        summary["warning"] = "no SCRIPT_LLM_API_KEY; rule-filter only"

    try:
        with profile_lock("foundit", timeout=600):
            deadline = time.time() + TIME_BUDGET  # budget starts once the profile is ours
            xvfb = virtual_display(start_xvfb)
            try:
                with sync_playwright() as p:
                    ctx = p.chromium.launch_persistent_context(
                        user_data_dir=PROFILE_DIR, channel="chrome", **headless_launch_kwargs(),
                        locale="en-IN", timezone_id="Asia/Kolkata",
                        viewport={"width": 1440, "height": 900},
                        args=["--disable-blink-features=AutomationControlled",
                              "--no-sandbox"])
                    page = ctx.new_page()
                    if not login_foundit(page):
                        summary["error"] = "FOUNDIT_LOGIN_FAILED"
                        print(json.dumps(summary, indent=2))
                        ctx.close()
                        return 2

                    for q, start in [(q, 15 * n) for q in SEARCH_QUERIES
                                     for n in range(SEARCH_PAGES)]:
                        if len(summary["applied"]) >= budget or time.time() > deadline:
                            break
                        url = (f"{BASE}/srp/results?query={q.replace(' ', '+')}"
                               f"&locations={LOCATIONS}" + (f"&start={start}" if start else ""))
                        try:
                            page.goto(url, timeout=45000, wait_until="domcontentloaded")
                            time.sleep(7)
                        except Exception as e:
                            print(f"[fi] error {q}: {e}", file=sys.stderr)
                            continue

                        cards = page.locator('[class*="srpResultCardContainer" i]')
                        n = cards.count()
                        print(f"[fi] {q} @{start}: {n} cards", file=sys.stderr)

                        for i in range(n):
                            if len(summary["applied"]) >= budget:
                                break
                            if time.time() > deadline:
                                summary["note"] = "stopped at time budget"
                                break
                            try:
                                card_el = cards.nth(i)
                                text = card_el.inner_text()
                                key = card_el.evaluate(
                                    """el => { const d = el.querySelector(
                                        'div[id]:not([id="jobCardTitle"])');
                                       return d ? d.id : null; }""")
                                if not key:
                                    continue
                                if seen_before(conn, key):
                                    summary["skipped_seen"] += 1
                                    continue
                                t_el = card_el.locator("#jobCardTitle").first
                                t = t_el.inner_text().strip() if t_el.count() else None
                                card = parse_card(text, title=t)
                                ok, why = experience_filter(card)
                                if not ok:
                                    st, det = "filtered", why
                                else:
                                    st, det = open_and_apply(page, card_el, card, DRY_RUN)
                                    # the detail view replaces the rail; go back for the next card
                                    page.goto(url, timeout=45000,
                                              wait_until="domcontentloaded")
                                    time.sleep(5)
                                    cards = page.locator(
                                        '[class*="srpResultCardContainer" i]')
                                # A dry run must not write: 'filtered' is a dedupe
                                # status, so recording a "would have applied" here
                                # permanently hides that job from every later run.
                                if not DRY_RUN:
                                    record(conn, key, url, card["title"],
                                           card["company"], st, det)
                                summary.setdefault(st, summary["failed"]).append(
                                    {"title": card["title"], "company": card["company"],
                                     "exp": f"{card['exp_min']}-{card['exp_max']}",
                                     "location": card["location"], "detail": det})
                                time.sleep(2)
                            except Exception as e:
                                print(f"[fi] card {i} error: {str(e)[:120]}",
                                      file=sys.stderr)
                    ctx.close()
            finally:
                xvfb.terminate()
    except Exception as e:
        summary["error"] = str(e)

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
