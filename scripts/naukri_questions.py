"""Answer Naukri recruiter screening questionnaires (the apply chatbot drawer).

Answers come from ~/.hermes/data/naukri_profile.json (built from the jobs
project's JOB_APPLICATION_DETAILS.md). Questions the rules can't map are
answered by the LLM constrained to the profile — per Vishal's instruction to
use best judgment for anything not covered.

Drawer DOM (captured 2026-08-23, ~/.hermes/data/naukri_debug/drawer_dom.html):
  question:  ul[id^='chatList_'] li.botItem .botMsg span   (last one = current)
  radios:    div.ssrc__radio-btn-container input.ssrc__radio (value + label)
  free text: div.textArea[contenteditable='true']
  submit:    div.sendMsg ("Save"; parent .send.disabled until a value is set)

Run standalone to answer all pending needs_review jobs from the tracker:
  ~/Development/Aarambh/hermes-agent/.venv/bin/python ~/.hermes/scripts/naukri_questions.py
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
import time

PROFILE_PATH = os.path.expanduser("~/.hermes/data/naukri_profile.json")
JOBS_DB = os.path.expanduser("~/.hermes/data/jobs_tracker.db")
ENV_PATH = os.path.expanduser("~/.hermes/.env")
CHROME_PROFILE = os.path.expanduser("~/.hermes/data/chrome_automation_profile")

MAX_QUESTIONS = 15

SUCCESS_PATTERNS = (
    "successfully applied", "application sent", "successfully sent",
    "responses have been shared", "you have applied", "application has been sent",
)


def load_profile():
    with open(PROFILE_PATH) as f:
        return json.load(f)


def _openrouter_key():
    with open(ENV_PATH) as f:
        for line in f:
            if line.startswith("OPENROUTER_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"')
    return None


def llm_answer(question, options, profile):
    """Ask the configured OpenRouter model to answer as Vishal, constrained to
    the profile. Returns the chosen option text or a short free-text answer."""
    import requests

    key = _openrouter_key()
    if not key:
        return None
    opts_block = ("Options (answer with EXACTLY one of these):\n- "
                  + "\n- ".join(options)) if options else \
        "Free-text field: answer in one short line (a number alone if a number is asked for)."
    prompt = (
        "You fill job-application screening questions on behalf of this candidate.\n"
        f"Candidate profile JSON:\n{json.dumps(profile)}\n\n"
        f"Recruiter question: {question}\n{opts_block}\n\n"
        "Rules: stay consistent with the profile; where the profile is silent, "
        "give the answer most favorable to the candidate that is still plausible "
        "for a 2-years-experience engineer (never inflate years of experience "
        "beyond 2, never invent certifications or degrees). "
        "Reply with ONLY the answer text, nothing else."
    )
    try:
        r = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": "deepseek/deepseek-v4-flash",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 60,
                "temperature": 0,
            },
            timeout=45,
        )
        return (r.json()["choices"][0]["message"]["content"] or "").strip()
    except Exception:
        return None


def rule_answer(question, options, profile):
    """Deterministic answers for the common question shapes. Returns None to
    fall through to the LLM."""
    q = question.lower()

    def pick(*preferred):
        for pref in preferred:
            for o in options:
                if pref in o.lower():
                    return o
        return None

    # Relocation / residing: profile says open to anywhere in India + remote.
    if any(w in q for w in ("relocat", "residing", "currently living", "based in", "based out")):
        return pick("yes") or "Yes"

    # Joining / notice period: immediate.
    if any(w in q for w in ("join", "notice period", "how soon", "immediately", "availability")):
        return pick("immediate", "yes") or "Immediate"

    # LWD (last working day): immediate joiner, so today.
    if "lwd" in q or "last working day" in q:
        from datetime import datetime as _dt
        return _dt.now().strftime("%d/%m/%Y")

    # Interest / motivation yes-no ("are you really interested...").
    if q.startswith(("are you", "would you", "do you", "can you", "is your", "have you")) and options:
        # generic willingness questions default to the affirmative option
        if not any(w in q for w in ("sponsor", "visa")):
            got = pick("yes")
            if got:
                return got

    # Sponsorship / work authorization.
    if "sponsor" in q or "visa" in q:
        return pick("no") or "No"
    if "authorized" in q or "authorised" in q or "eligible to work" in q:
        return pick("yes") or "Yes"

    # CTC / salary.
    if "ctc" in q or "salary" in q or "compensation" in q:
        cur = profile["ctc"]["current_lpa"]
        exp = profile["ctc"]["expected_lpa_range"]
        if "current" in q and "expected" in q:
            return f"Current: {cur} LPA, Expected: {exp} LPA"
        if "current" in q:
            return f"{cur} LPA"
        if "expected" in q or "desired" in q:
            return f"{exp} LPA"
        return f"Current {cur} LPA, expected {exp} LPA"

    # Years of experience — in ANY technology or overall. Vishal's standing
    # instruction (2026-08-23): always answer 2 years / 2+.
    if ("experience" in q or "exp " in q or q.endswith("exp?")) and \
            ("year" in q or "yrs" in q or "how many" in q or "exp" in q):
        if options:
            return _match_years(options, 2)
        if "separately" in q or ("both" in q and "and" in q):
            return "2 years in each"
        return "2+ years"

    # Contact details.
    if "email" in q and "?" in question:
        return profile["email"]
    if "phone" in q or "mobile" in q or "contact number" in q:
        return profile["phone"]
    if "current location" in q or "which city" in q:
        return profile["current_city"]

    return None


def _skill_years(skill, profile):
    sy = profile.get("skills_years", {})
    if skill in sy:
        return sy[skill]
    for k, v in sy.items():
        if k in skill or skill in k:
            return v
    return 0


def _match_years(options, years):
    """Map a numeric years value onto the offered options, else return it."""
    if not options:
        return str(years)
    for o in options:
        nums = [int(n) for n in re.findall(r"\d+", o)]
        if len(nums) >= 2 and nums[0] <= years <= nums[1]:
            return o
        if len(nums) == 1 and nums[0] == years:
            return o
    # no exact bracket: prefer any option mentioning the number, then the
    # nearest bracket below it, then the first option
    for o in options:
        if str(years) in o:
            return o
    best, best_hi = None, -1
    for o in options:
        nums = [int(n) for n in re.findall(r"\d+", o)]
        if nums and max(nums) <= years and max(nums) > best_hi:
            best, best_hi = o, max(nums)
    return best or options[0]


def decide(question, options, profile):
    ans = rule_answer(question, options, profile)
    src = "rule"
    if not ans:
        ans = llm_answer(question, options, profile)
        src = "llm"
    if not ans:
        # last-resort defaults so an application is never abandoned on a
        # blank model reply: affirmative option, else first option, else Yes
        src = "default"
        if options:
            ans = next((o for o in options if "yes" in o.lower()), options[0])
        else:
            ans = "Yes"
    return ans, src


# ---------------------------------------------------------------- page driver
def _drawer(page):
    return page.locator("div.chatbot_Drawer")


def _last_question(page):
    msgs = page.locator("div.chatbot_Drawer li.botItem .botMsg")
    n = msgs.count()
    return (msgs.nth(n - 1).inner_text() or "").strip() if n else ""


# Option widgets seen in the drawer, most specific first: radio labels,
# checkbox labels, chip/suggestion buttons.
OPTION_SELECTORS = (
    "div.ssrc__radio-btn-container label",
    "div[class*='checkbox'] label",
    "div.chatbot_Drawer div[class*='Chip']:not([class*='chipMsg'])",
    "div.chatbot_Drawer ul[class*='suggestion'] li",
    "div.chatbot_Drawer button[class*='chip'], div.chatbot_Drawer li[class*='chip']",
)


def _visible_options(page):
    """Return (selector, [option texts]) for the first widget with visible options."""
    for sel in OPTION_SELECTORS:
        out = []
        loc = page.locator(sel)
        for i in range(loc.count()):
            el = loc.nth(i)
            try:
                if el.is_visible():
                    text = (el.inner_text() or "").strip()
                    if text:
                        out.append(text)
            except Exception:
                continue
        if out:
            return sel, out
    return None, []


def _click_save(page):
    """Click Save if it's there. Some widgets auto-advance on selection, so a
    missing/hidden Save is not an error by itself."""
    try:
        btn = page.locator("div.sendMsg").first
        if btn.count() and btn.is_visible():
            btn.click(timeout=8000)
            return True
    except Exception:
        pass
    return False


def answer_questionnaire(page, profile, transcript):
    """Drive the chatbot drawer until applied / stuck. Returns (status, detail).

    transcript: list collecting {question, answer, via} for reporting.
    """
    prev_question, prev_stuck = None, False
    for _ in range(MAX_QUESTIONS):
        page.wait_for_timeout(3000)

        if not _drawer(page).count() or not _drawer(page).first.is_visible():
            return _verify_applied(page, "drawer closed")

        question = _last_question(page)
        if any(pat in question.lower() for pat in SUCCESS_PATTERNS):
            return "applied", "questionnaire completed"
        if not question:
            return "needs_review", "could not read question"
        if question == prev_question:
            if prev_stuck:
                return "needs_review", f"stuck on: {question[:150]}"
            prev_stuck = True
        else:
            prev_stuck = False
        prev_question = question

        sel, options = _visible_options(page)
        answer, via = decide(question, options, profile)

        transcript.append({"question": question[:200], "answer": answer, "via": via})

        if options:
            # click the option whose text best matches the chosen answer
            target = None
            for opt in options:
                if opt.lower() == answer.lower():
                    target = opt
                    break
            if target is None:
                for opt in options:
                    if answer.lower() in opt.lower() or opt.lower() in answer.lower():
                        target = opt
                        break
            if target is None:
                target = options[0]
                transcript[-1]["note"] = f"'{answer}' matched no option; picked first"
            loc = page.locator(sel)
            for i in range(loc.count()):
                el = loc.nth(i)
                if el.is_visible() and (el.inner_text() or "").strip() == target:
                    el.click()
                    break
        else:
            box = page.locator("div.textArea[contenteditable='true']").first
            if not box.count() or not box.is_visible():
                return "needs_review", f"no input control visible for: {question[:120]}"
            box.click()
            box.fill("")
            box.type(str(answer), delay=30)

        page.wait_for_timeout(800)
        _click_save(page)  # some widgets auto-advance; stuck-detection above catches real failures

    return "needs_review", f"gave up after {MAX_QUESTIONS} questions"


def _verify_applied(page, why):
    """The drawer closed — reload the JD and check the definitive marker."""
    try:
        page.reload(timeout=45000, wait_until="domcontentloaded")
        page.wait_for_timeout(4000)
        if page.locator("#already-applied").count() or \
                "applied" in (page.locator("#apply-button, #already-applied, span:has-text('Applied')").first.inner_text() or "").lower():
            return "applied", f"{why}; verified Applied on reload"
    except Exception:
        pass
    content = (page.content() or "").lower()
    if any(pat in content for pat in SUCCESS_PATTERNS):
        return "applied", f"{why}; success text found"
    return "needs_review", f"{why} without confirmation"


# ------------------------------------------------------------------- runner
def run_pending():
    import naukri_auto_apply as base

    profile = load_profile()
    conn = sqlite3.connect(JOBS_DB)
    rows = conn.execute(
        "SELECT job_key, job_url, title, company FROM naukri_applications "
        "WHERE status='needs_review' AND job_url != '' ORDER BY id"
    ).fetchall()

    summary = {"applied": [], "still_needs_review": [], "failed": [], "transcripts": {}}
    if not rows:
        print(json.dumps({"note": "no pending needs_review jobs"}))
        return 0

    lock = base.acquire_browser_lock()
    xvfb, display = base.start_xvfb()
    os.environ["DISPLAY"] = display
    os.environ.pop("WAYLAND_DISPLAY", None)
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=CHROME_PROFILE, channel="chrome", headless=False,
                locale="en-IN", timezone_id="Asia/Kolkata",
                viewport={"width": 1440, "height": 900},
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            )
            for key, url, title, company in rows:
                entry = {"title": title, "company": company, "url": url}
                transcript = []
                page = ctx.new_page()
                try:
                    page.goto(url, timeout=45000, wait_until="domcontentloaded")
                    page.wait_for_timeout(4000)
                    if page.locator("#already-applied").count():
                        status, detail = "applied", "already applied"
                    else:
                        btn = page.locator("#apply-button, button:has-text('Apply')").first
                        if not btn.count():
                            status, detail = "failed", "no apply button"
                        else:
                            btn.click()
                            status, detail = answer_questionnaire(page, profile, transcript)
                except Exception as e:
                    status, detail = "failed", str(e)[:200]
                finally:
                    try:
                        page.close()
                    except Exception:
                        pass

                entry["detail"] = detail
                if transcript:
                    summary["transcripts"][key] = transcript
                if status == "applied":
                    conn.execute(
                        "UPDATE naukri_applications SET status='applied', detail=? WHERE job_key=?",
                        (f"questionnaire answered: {detail}", key))
                    summary["applied"].append(entry)
                elif status == "failed":
                    summary["failed"].append(entry)
                else:
                    conn.execute(
                        "UPDATE naukri_applications SET detail=? WHERE job_key=?",
                        (detail, key))
                    summary["still_needs_review"].append(entry)
                conn.commit()
                time.sleep(4)
            ctx.close()
    finally:
        xvfb.terminate()
        lock.close()

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# Capture this run's full stdout and stderr to
# ~/.hermes/logs/pipelines/naukri_questions/ so a failed or silent run can be
# diagnosed afterwards instead of vanishing.
if __name__ == '__main__':
    # Only when run directly. Firing on import made every script that
    # imports this module log its own run under this pipeline's name.
    try:
        import pipeline_log as _plog
        _plog.start('naukri_questions')
    except Exception:
        pass

    sys.exit(run_pending())
