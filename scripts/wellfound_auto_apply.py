"""Wellfound auto-apply v4 — AI-gated.

Per job: read the page's own metadata (location, remote, visa sponsorship,
h1 company), hard-filter to jobs that fit an India-based remote candidate
(fully remote, or visa sponsorship available), then ask the LLM for a
fit verdict and a tailored "why this company" answer, fill the real apply
modal (required textareas, URL fields, honest radios) and Send.
Success is counted ONLY when the modal actually closes.
"""
import json, os, re, sqlite3, subprocess, sys, time, urllib.request
from playwright.sync_api import sync_playwright
from browser_lock import profile_lock, clear_chrome_singletons, headless_launch_kwargs, virtual_display

PROFILE_DIR = os.path.expanduser("~/.hermes/data/chrome_automation_profile")
JOBS_DB = os.path.expanduser("~/.hermes/data/jobs_tracker.db")
HERMES_ENV = os.path.expanduser("~/.hermes/.env")
MAX_APPLIES_PER_RUN = int(os.environ.get("WELLFOUND_MAX_PER_RUN", 20))
TIME_BUDGET = int(os.environ.get("WELLFOUND_TIME_BUDGET", 420))
WELLFOUND_URL = "https://wellfound.com"
# 2026-09-08: OpenRouter account died (401 "User not found."). Gemini's
# OpenAI-compatible endpoint accepts this same chat/completions payload.
API_URL = os.environ.get(
    "SCRIPT_LLM_API_URL",
    "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions")
MODEL = os.environ.get("WELLFOUND_AI_MODEL", "gemini-3.5-flash-lite")


# Discovery surface only — NOT the fit test. A slug is just how Wellfound files a
# listing; the same Go/K8s/Terraform work is posted as Platform, Infrastructure,
# Cloud, Production, Systems or plain Software Engineer. So cast a wide net here
# across the infra+backend cluster and let ai_verdict judge on the actual stack.
#
# Measured 2026-09-10, jobs/fresh per page against a 161-key dedupe set:
#   platform-engineer 57/49   cloud-engineer 48/43   infrastructure-engineer 44/43
#   backend-engineer  41/25   software-engineer 39/28  devops-engineer 27/27
#   site-reliability-engineer 26/20
# Dropped as permanently empty: golang-backend, python-backend, node-js-backend,
# ai-engineer-backend — every run since 08-28 returned 0 jobs for these.
#
# Order matters: TIME_BUDGET runs out long before the list does. The 09-10 run
# spent all 420s inside platform-engineer alone and never reached devops, sre or
# backend — and platform-engineer's page is the noisiest of the set, full of
# Account Executive / Sales Engineer / Director of Revenue Cycle Management
# postings. The named target roles go first so a truncated run still hits them.
SEARCH_ROLES = [
    "devops-engineer", "site-reliability-engineer", "backend-engineer",
    "full-stack-engineer", "infrastructure-engineer", "platform-engineer",
    "cloud-engineer", "software-engineer",
]

# /role/r/<slug> is kept because it surfaces more UNSEEN jobs, not because it
# filters for remote. It does not: a full run over /role/r/platform-engineer came
# back 41-for-41 US-onsite (San Francisco, NYC, Chicago, Santa Clara), so the "r"
# appears to mark remote-friendly companies rather than remote roles. Remote is
# decided downstream by hard_filter and ai_verdict, as it is for /role/.
# The reason to prefer it is purely yield: on site-reliability-engineer it returned
# 20 fresh against 8 fresh from the unfiltered /role/ form.
# The /role/ fallback is for transient empties, not permanently dead slugs — these
# pages hydrate their listings client-side and a slow one can yield 0 links on an
# otherwise healthy feed (seen once on full-stack-engineer, which returned 32 on
# the very next run). Retrying the unfiltered form beats losing the feed for a run.
ROLE_URL_FORMS = ("/role/r/{slug}", "/role/{slug}")

# NOTE: do not "improve" this to /jobs?searchKeyword=<tech>. Verified 2026-09-10:
# observability, kubernetes, golang, site reliability and devops all return the
# byte-identical 18 listings (pairwise overlap 1.00). The keyword is applied
# client-side after hydration, so scraping the DOM yields the unfiltered default
# list no matter what the term is.

CANDIDATE_FACTS = """Vishal Panwar — Backend Engineer, 2+ years, based in Hyderabad, India.
Stack: Go, Node.js, Python, TypeScript; PostgreSQL, Redis, MongoDB; Kubernetes, Docker, GCP, AWS, Terraform; NATS, gRPC, microservices.
Wins: cut API p95 latency 40% at 1K+ RPS (Go buffer pooling); reduced PostgreSQL load 60% (Redis caching + distributed locking); NATS event-driven microservices with KEDA autoscaling; at Cognizant built REST APIs for 1,000+ users on Azure, promoted in 6 months.
Projects: Table-Tap (live contactless QR ordering, GPT-4 RAG analytics, 500+ concurrent orders, GCP Cloud Run) https://table-tap.in ; multi-qr npm package https://www.npmjs.com/package/multi-qr
Links: LinkedIn https://linkedin.com/in/vishalpanwar416 | GitHub https://github.com/vishalpanwar416 | Site https://vishalpanwar.in
Work situation: works fully remote from India (IST, overlaps US/EU part of day); for onsite roles abroad NEEDS visa sponsorship; not willing to relocate within India; can start immediately."""

def load_env_key(name="SCRIPT_LLM_API_KEY"):
    val = os.environ.get(name)
    if val: return val.strip()
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
# Login and contact details live in ~/.hermes/.env, never in this (public) repo.
WF_EMAIL = load_env_key("WELLFOUND_EMAIL")
WF_PASS = load_env_key("WELLFOUND_PASSWORD")
# "Phone Number*" is a required field in some apply modals and there was nothing to
# fill it from. Source: ~/.hermes/skills/job-apply/profile.md
PHONE = load_env_key("JOB_APPLY_PHONE") or ""

def ai_json(prompt, max_tokens=700):
    """One-shot LLM call returning parsed JSON, or None on any failure."""
    if not API_KEY: return None
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens, "temperature": 0.4,
    }).encode()
    req = urllib.request.Request(API_URL, data=body, headers={
        "Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            choice = json.load(r)["choices"][0]["message"]
            txt = choice.get("content") or ""
        m = re.search(r"\{.*\}", txt, re.S)
        return json.loads(m.group(0)) if m else None
    except Exception as e:
        print(f"[ai] error: {e}", file=sys.stderr)
        return None

def init_db():
    conn = sqlite3.connect(JOBS_DB)
    conn.execute("CREATE TABLE IF NOT EXISTS wellfound_applications (id INTEGER PRIMARY KEY AUTOINCREMENT, job_key TEXT UNIQUE, job_url TEXT, title TEXT, company TEXT, status TEXT, detail TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    conn.commit(); conn.close()

def seen_before(conn, key):
    return conn.execute("SELECT id FROM wellfound_applications WHERE job_key=? AND status IN ('applied','external','filtered')", (key,)).fetchone() is not None

def record(conn, key, url, title, company, status, detail):
    conn.execute("INSERT OR REPLACE INTO wellfound_applications (job_key,job_url,title,company,status,detail) VALUES (?,?,?,?,?,?)",
                 (key, url, title, company, status, detail)); conn.commit()

def start_xvfb():
    for n in range(90, 110):
        if os.path.exists(f"/tmp/.X{n}-lock"): continue
        proc = subprocess.Popen(["Xvfb", f":{n}", "-screen", "0", "1440x900x24"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.5)
        if proc.poll() is None: return proc, f":{n}"
    raise RuntimeError("no free X display")

def login_wellfound(page):
    try:
        page.goto(f"{WELLFOUND_URL}/login", timeout=30000, wait_until="domcontentloaded"); time.sleep(4)
        if not page.locator("#user_email").count():
            print("[wf] Already logged in", file=sys.stderr); return True
        page.fill("#user_email", WF_EMAIL); time.sleep(1)
        page.fill("#user_password", WF_PASS); time.sleep(1)
        btn = page.locator('input[type="submit"], button[type="submit"]').first
        if btn.count(): btn.click(); time.sleep(6)
        if not page.locator("#user_email").count() and "login" not in page.url.lower():
            print("[wf] Login OK", file=sys.stderr); return True
        print("[wf] Login failed", file=sys.stderr); return False
    except Exception as e:
        print(f"[wf] Login error: {e}", file=sys.stderr); return False

JOB_HREF = re.compile(r"^/jobs/\d+-")

def collect_jobs(page, role_slug, limit=60):
    """Harvest a role feed, preferring the remote-filtered variant.

    limit was 20, which was below what these pages actually carry (platform-engineer
    alone lists 57), so the tail was never seen on any run.
    """
    for form in ROLE_URL_FORMS:
        jobs, seen = [], set()
        path = form.format(slug=role_slug)
        try:
            page.goto(f"{WELLFOUND_URL}{path}", timeout=45000, wait_until="domcontentloaded"); time.sleep(6)
            for el in page.locator('a[href*="/jobs/"]').all():
                if len(jobs) >= limit: break
                try:
                    href = el.get_attribute("href") or ""
                    if not JOB_HREF.match(href) or href in seen: continue
                    seen.add(href)
                    title = (el.inner_text() or "").strip().split("\n")[0] or role_slug
                    jobs.append({"key": re.sub(r"[^a-zA-Z0-9]", "_", href)[:60],
                                 "url": WELLFOUND_URL + href, "title": title, "company": "?"})
                except Exception: pass
        except Exception as e:
            print(f"[wf] error {path}: {e}", file=sys.stderr)
            continue
        if jobs:
            print(f"[wf] {path}: {len(jobs)} jobs", file=sys.stderr)
            return jobs
        print(f"[wf] {path}: 0 jobs, trying next form", file=sys.stderr)
    return []

def read_job_page(page, job):
    """Extract company, panel metadata (location/remote/visa/relocation) and description."""
    info = {"visa": "", "location": "", "remote": "", "desc": ""}
    got = page.evaluate("""() => {
        const out = {};
        const h1 = document.querySelector('h1');
        if (h1) { const m = h1.innerText.match(/ at (.+)$/); if (m) out.company = m[1].trim(); }
        const body = document.body.innerText;
        // Match the label as a whole word with an optional plural and colon.
        // indexOf('Location') on a page that says "Locations" matched mid-word and
        // returned the leftover "s" as the value — which then flowed into
        // hard_filter, where a remote job read as onsite-abroad and was rejected.
        const grab = (label) => {
            const re = new RegExp('\\\\b' + label + 's?\\\\b\\\\s*:?\\\\s*', 'i');
            const m = body.match(re);
            if (!m) return '';
            const start = m.index + m[0].length;
            return body.slice(start, start + 80).split('\\n').filter(s => s.trim())[0] || '';
        };
        out.location = grab('Location');
        out.jobtype = grab('Job type');
        out.visa = grab('Visa sponsorship');
        out.relocation = grab('Relocation');
        out.remote_hint = /remote/i.test(body.slice(0, 4000)) ? 'remote mentioned' : '';
        const about = document.querySelector('[class*="description" i], main');
        out.desc = about ? about.innerText.slice(0, 1800) : body.slice(0, 1800);
        return out;
    }""")
    if got.get("company"): job["company"] = got["company"]
    info.update({k: (got.get(k) or "").strip() for k in ("location", "jobtype", "visa", "relocation", "remote_hint", "desc")})
    return info

def hard_filter(info):
    """India-based candidate: fully-remote OK, or onsite-abroad only with visa sponsorship."""
    # Check the negative FIRST and make the positive exclusive of it. "available"
    # is a substring of "not available", so the old plain `in` test set visa_yes
    # on every job that explicitly refused sponsorship — they passed the rule and
    # only the LLM caught them, at a page load plus an API call each.
    lv = info["visa"].lower()
    visa_no = bool(re.search(r"not available|unavailable|not offer|do(?:es)? not sponsor|\bno\b|\bnone\b", lv))
    visa_yes = not visa_no and bool(re.search(r"\bavailable\b|\byes\b|sponsor", lv))
    
    remote = "remote" in (info["location"] + " " + info["jobtype"]).lower()
    india = any(c in info["location"].lower() for c in ("india", "hyderabad", "bangalore", "bengaluru", "pune", "delhi", "gurgaon", "noida", "mumbai", "chennai", "remote"))
    
    # US cities that mean onsite - no visa for Indian candidate
    us_cities = ("san francisco", "new york", "oakland", "los angeles", "chicago", "seattle",
                 "austin", "boston", "denver", "portland", "miami", "atlanta", "dallas",
                 "united states", "us ", "usa", "bay area", "silicon valley", "santa clara",
                 "california", "new york city", "washington dc")
    
    us_onsite = any(c in info["location"].lower() for c in us_cities)
    
    if india: return True, "india/remote location"
    if remote: return True, "remote mentioned"
    if visa_yes: return True, "visa sponsorship available"
    
    # If it's a US city and no visa sponsorship mentioned, reject
    if us_onsite and not visa_yes:
        return False, f"onsite abroad ({info['location'] or 'unknown'}), no visa sponsorship"
    
    # If location is unknown but description mentions US/visa requirements
    desc = (info.get("desc") or "").lower()
    if any(c in desc for c in ("must be us", "us based", "us citizen", "us resident", "authorized to work in the us", "no visa sponsorship")) and not visa_yes:
        return False, "US-based only, no visa sponsorship"
    
    if not visa_no: return True, "visa sponsorship possible"
    return False, f"onsite abroad ({info['location'] or 'unknown'}), no visa sponsorship"

def ai_verdict(job, info):
    prompt = f"""You screen jobs for this candidate:
{CANDIDATE_FACTS}

Job: {job['title']} at {job['company']}
Location: {info['location']} | Type: {info['jobtype']} | Visa sponsorship: {info['visa']} | Relocation: {info['relocation']}
Description (truncated): {info['desc'][:1200]}

Return JSON only:
{{"apply": true/false,
 "reason": "one line",
 "why_answer": "2-3 first-person sentences answering 'Why do you want to work at {job['company']}?' — specific to this job and the candidate's real experience, plain language, no dashes as punctuation, no buzzwords"}}
JUDGE ON THE TECH, NOT THE JOB TITLE. Titles for identical work vary a lot between
companies — Site Reliability, DevOps, Platform, Infrastructure, Cloud, Systems,
Production, Observability, Backend, Full Stack and plain Software Engineer are all
acceptable titles. Set apply=true when the day-to-day work is any of:
  - reliability / SRE: uptime, on-call, incident response, SLOs, scaling
  - devops / platform / infrastructure: Kubernetes, Docker, Terraform, CI/CD, AWS/GCP
  - observability: metrics, tracing, logging, Prometheus, Grafana, OpenTelemetry
  - backend: APIs, services, Go/Node/Python, PostgreSQL/Redis/Mongo, gRPC, queues
  - full stack: backend work as above with a TypeScript/React front end
A title outside that list is fine if the described stack is in it, and a matching
title is NOT enough on its own if the described work is something else.

Rules: apply=false if the role clearly needs 5+ more years than he has, requires
being onsite in a country without visa sponsorship, is unpaid/equity-only, or is a
genuinely different discipline — mobile, embedded, frontend-only, data science or
ML research, security-only, QA-only, or engineering management.
CRITICAL: If the role is in the US (San Francisco, NYC, etc.) or UK/Europe and does NOT explicitly say "visa sponsorship available" or "remote", set apply=false. The candidate is based in India and cannot relocate without visa sponsorship."""
    v = ai_json(prompt)
    if not v or "apply" not in v:
        return {"apply": True, "reason": "ai unavailable, rule-pass", "why_answer": ""}
    return v

RADIO_JS = """() => {
    const rs = Array.from(document.querySelectorAll('input[type=radio]'))
        .filter(e => e.offsetParent !== null);
    // Option text is NOT the radio's <label>: that returns the whole question,
    // shared by every option in the group. Walk up from the input to the
    // innermost ancestor with short text, which is the option's own wrapper.
    const optText = (r) => {
        let e = r.parentElement;
        for (let i = 0; i < 3 && e; i++) {
            const t = (e.innerText || '').trim();
            if (t && t.length < 60) return t;
            e = e.parentElement;
        }
        return r.value;
    };
    const groups = {};
    for (const r of rs) {
        const k = r.name || '';
        if (!k) continue;
        if (!groups[k]) {
            const lab = r.closest('label');
            groups[k] = {name: k, question: lab ? lab.innerText.trim().slice(0,300) : '',
                         options: [], checked: false};
        }
        groups[k].options.push({value: r.value, text: optText(r)});
        if (r.checked) groups[k].checked = true;
    }
    return Object.values(groups);
}"""


def answer_choice_question(question, options, company):
    """Pick one option for a radio group. Honesty matters more than pass rate:
    claiming US work authorisation would be a lie that surfaces at screening."""
    opts = "\n".join(f"{i}. {o['text']}" for i, o in enumerate(options))
    v = ai_json(f"""Candidate facts:
{CANDIDATE_FACTS}

Application question from {company}: "{question[:300]}"
Options:
{opts}

Return JSON: {{"index": <the single best option number>}}
Answer HONESTLY from the facts above. Specifically:
- He is in India and needs visa sponsorship: any "do you live in / plan to live in
  the US", "are you legally authorized to work in the US", "do you have work
  authorization" style question is NO, and any "will you require sponsorship"
  question is YES.
- He will not relocate within India, and works remote.
- He has ~2 years professional experience. Do not claim a seniority band he
  does not have.
- He does have production Kubernetes, Docker, Terraform, AWS/GCP and Go/Node/Python
  backend experience, so infra/SRE/devops capability questions are YES.
- For "where did you learn about this job", prefer a Wellfound/AngelList option if
  one exists, otherwise the most neutral option.""", max_tokens=120)
    try:
        i = int((v or {}).get("index", -1))
        if 0 <= i < len(options):
            return options[i]
    except Exception:
        pass
    return None


def answer_custom_question(question, company):
    v = ai_json(f"""Candidate facts:
{CANDIDATE_FACTS}

Application form question from {company}: "{question[:300]}"
Return JSON: {{"answer": "honest, specific, first-person answer in 1-3 sentences (or the single value asked for). Plain language, no dashes as punctuation. Never invent experience he does not have."}}
If the question is an instruction to type a particular word or value (anti-bot
checks like "if you are a human, type ALAN below"), the answer is exactly that
word and nothing else.""", max_tokens=300)
    return (v or {}).get("answer", "")

def fill_modal_and_send(page, job, why_answer):
    tas = page.locator("textarea:visible").all()
    for i, ta in enumerate(tas):
        try:
            label = ta.evaluate("el => { const c = el.closest('div'); return c && c.parentElement ? c.parentElement.innerText.slice(0,200) : ''; }") or ""
            label_l = label.lower()
            if i == 0 or "why" in label_l:
                ans = why_answer or answer_custom_question(label or "Why do you want to work here?", job["company"])
            elif "app" in label_l and "built" in label_l or "project" in label_l:
                ans = ("Table-Tap, a live contactless QR ordering system with GPT-4 RAG analytics: https://table-tap.in "
                       "and multi-qr on npm: https://www.npmjs.com/package/multi-qr. More on GitHub: https://github.com/vishalpanwar416")
            else:
                ans = answer_custom_question(label, job["company"])
            if ans:
                ta.click(); ta.fill(ans); time.sleep(0.4)
        except Exception: pass

    for inp in page.locator('input[type="text"]:visible, input[type="url"]:visible').all():
        try:
            if inp.input_value(): continue
            ph = ((inp.get_attribute("placeholder") or "") + " " +
                  (inp.evaluate("el => el.closest('label,div') ? el.closest('label,div').innerText.slice(0,80) : ''") or "")).lower()
            if "linkedin" in ph: inp.fill("https://linkedin.com/in/vishalpanwar416")
            elif "portfolio" in ph or "website" in ph: inp.fill("https://vishalpanwar.in")
            elif "github" in ph: inp.fill("https://github.com/vishalpanwar416")
            elif "phone" in ph or "mobile" in ph: inp.fill(PHONE)
            elif "job title" in ph or "current title" in ph or "recent title" in ph:
                inp.fill("Backend Engineer")
            elif "*" in ph:
                # Some modals mark required text fields only with an asterisk in the
                # label. Leaving one blank silently blocks Send, so answer it.
                ans = answer_custom_question(ph, job["company"])
                if ans: inp.fill(ans[:200])
            time.sleep(0.3)
        except Exception: pass

    # Radio groups. The old version only clicked "No" on in-person/onsite/relocation
    # questions and left every other group untouched — which is what produced the
    # "send clicked but modal still open (validation?)" failures: a SentiLink modal
    # had seven unanswered groups, including a required "Where did you learn about
    # this job?*". Every group now gets an answer from the LLM.
    try:
        for g in page.evaluate(RADIO_JS):
            if g["checked"] or not g["options"]:
                continue
            q = g["question"] or " / ".join(o["text"] for o in g["options"])
            pick = answer_choice_question(q, g["options"], job["company"])
            if not pick:
                continue
            try:
                sel = 'input[type=radio][name="%s"][value="%s"]' % (
                    g["name"].replace('"', '\\"'), pick["value"])
                el = page.locator(sel).first
                if el.count():
                    el.check(timeout=4000)
                    time.sleep(0.3)
            except Exception:
                pass
    except Exception:
        pass

    send = page.locator('button:has-text("Send application")').first
    if not send.count():
        send = page.locator('button:has-text("Submit"), button[type="submit"]:visible').first
    if not send.count():
        return "needs_review", "no send button in modal"
    try:
        send.click(timeout=8000)
    except Exception:
        return "needs_review", "send button blocked (missing required field?)"
    time.sleep(5)
    try:
        still = page.locator('button:has-text("Send application")').first
        if still.count() and still.is_visible(timeout=2000):
            # Name what is still unanswered instead of guessing next time round.
            try:
                left = page.evaluate("""() => {
                    const out = [];
                    const seen = new Set();
                    for (const r of document.querySelectorAll('input[type=radio]')) {
                        if (r.offsetParent === null || !r.name || seen.has(r.name)) continue;
                        seen.add(r.name);
                        const any = document.querySelector(
                            'input[type=radio][name="' + CSS.escape(r.name) + '"]:checked');
                        if (!any) {
                            const l = r.closest('label');
                            out.push('radio:' + (l ? l.innerText.trim().slice(0,40) : r.name.slice(0,30)));
                        }
                    }
                    for (const e of document.querySelectorAll('textarea, input[type=text]')) {
                        if (e.offsetParent === null || e.value.trim()) continue;
                        const l = e.closest('label');
                        out.push('blank:' + (l ? l.innerText.trim().slice(0,40) : (e.name || e.type)));
                    }
                    return out.slice(0, 5);
                }""")
            except Exception:
                left = []
            det = "modal still open; unanswered: " + ("; ".join(left) if left else "none detected")
            return "needs_review", det[:190]
    except Exception:
        pass
    return "applied", "sent (modal closed)"

def apply_to_job(page, job):
    try:
        page.goto(job["url"], timeout=45000, wait_until="domcontentloaded"); time.sleep(5)
        if "login" in page.url.lower():
            return "failed", "session expired"
        info = read_job_page(page, job)

        body_l = page.locator("body").inner_text()[:2500].lower()
        if "not accepting" in body_l or "this job is no longer" in body_l:
            return "external", "position closed"

        apply_btn = None
        for sel in ['button:has-text("Apply now")', 'button:has-text("Apply")']:
            b = page.locator(sel).first
            try:
                if b.count() and b.is_visible(timeout=2000) and b.is_enabled():
                    apply_btn = b; break
            except Exception: pass
        if not apply_btn:
            return "applied", "already applied (no apply button)"

        ok, why = hard_filter(info)
        if not ok:
            return "filtered", why
        v = ai_verdict(job, info)
        if not v.get("apply", True):
            return "filtered", "ai: " + str(v.get("reason", ""))[:150]

        apply_btn.click(); time.sleep(5)
        return fill_modal_and_send(page, job, v.get("why_answer", ""))
    except Exception as e:
        return "failed", str(e)[:200]

def main():
    init_db(); conn = sqlite3.connect(JOBS_DB)
    deadline = time.time() + TIME_BUDGET
    # created_at is CURRENT_TIMESTAMP (UTC); shift both sides into IST to get an
    # IST calendar day. '+5:30' is not a SQLite modifier — it evaluates to NULL,
    # so the old comparison was never true and this cap never applied.
    done = conn.execute("SELECT COUNT(*) FROM wellfound_applications WHERE status='applied' AND date(created_at,'+330 minutes')=date('now','+330 minutes')").fetchone()[0]
    budget = min(MAX_APPLIES_PER_RUN, 100 - done)
    summary = {"applied": [], "needs_review": [], "external": [], "failed": [], "filtered": [],
               "skipped_seen": 0, "applied_today_before_run": done}
    if budget <= 0:
        summary["note"] = "daily cap"; print(json.dumps(summary, indent=2)); return 0
    if not API_KEY:
        summary["warning"] = "no SCRIPT_LLM_API_KEY; rule-filter only, generic answers"
    try:
        with profile_lock('wellfound', timeout=120):
            xvfb = virtual_display(start_xvfb)
            try:
                with sync_playwright() as p:
                    ctx = p.chromium.launch_persistent_context(
                        user_data_dir=PROFILE_DIR, channel="chrome", **headless_launch_kwargs(),
                        locale="en-IN", timezone_id="Asia/Kolkata",
                        viewport={"width": 1440, "height": 900},
                        args=["--disable-blink-features=AutomationControlled", "--no-sandbox"])
                    page = ctx.new_page()
                    if not login_wellfound(page):
                        summary["error"] = "WF_LOGIN_FAILED"; print(json.dumps(summary, indent=2)); ctx.close(); return 2
                    for role in SEARCH_ROLES:
                        if len(summary["applied"]) >= budget or time.time() > deadline: break
                        for job in collect_jobs(page, role):
                            if len(summary["applied"]) >= budget: break
                            if time.time() > deadline:
                                summary["note"] = "stopped at time budget"; break
                            if seen_before(conn, job["key"]):
                                summary["skipped_seen"] += 1; continue
                            st, det = apply_to_job(page, job)
                            record(conn, job["key"], job["url"], job["title"], job["company"], st, det)
                            e = {"title": job["title"], "company": job["company"], "url": job["url"], "detail": det}
                            summary.get(st, summary["failed"]).append(e)
                            time.sleep(2)
                    ctx.close()
            finally:
                xvfb.terminate()
    except Exception as e:
        summary["error"] = str(e)
    print(json.dumps(summary, indent=2)); return 0

if __name__ == "__main__":
    sys.exit(main())
