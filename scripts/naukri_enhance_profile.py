"""Enhance Naukri profile for maximum recruiter visibility and inbound offers.

Does everything in one pass:
1. Updates headline from "Key skills" to a proper title
2. Adds missing skills (FastAPI, Rest API, gRPC, SRE)
3. Updates profile summary for recruiter search keywords
4. Re-uploads the best resume PDF
5. Refreshes profile freshness
"""
import os
import sys
import json
import time
import re
import hashlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from playwright.sync_api import sync_playwright

PROFILE_DIR = os.path.expanduser("~/.hermes/data/chrome_automation_profile")
PROFILE_URL = "https://www.naukri.com/mnjuser/profile"
RESUME_BASE = os.path.expanduser("~/Development/Aarambh/jobs/resumes")

# Best resume for backend roles
BEST_RESUME = os.path.join(RESUME_BASE, "VishalPanwar_sde.pdf")

# New headline
NEW_HEADLINE = "Backend Engineer | Golang, Node.js, Python | Kubernetes, Docker, GCP & AWS | Microservices, Distributed Systems | 2+ Years Experience"

# Skills to add
SKILLS_TO_ADD = ["FastAPI", "Rest API", "gRPC", "SRE"]

# Profile summary text to set (rich with recruiter-search keywords)
PROFILE_SUMMARY = """Backend Engineer with 2+ years of experience building distributed systems, REST APIs, and microservices at scale. Proficient in Golang, Node.js, and Python with deep expertise in Kubernetes, Docker, GCP, and AWS cloud infrastructure. Designed and maintained high-throughput backend services handling 1K+ RPS with sub-50ms p99 latency through buffer pooling, caching strategies, and message queue architectures. Built production-grade Redis caching layers with distributed locking, cache stampede prevention, and TTL-based expiration. Implemented disaster recovery runbooks, automated CI/CD pipelines, and Prometheus/Grafana observability stacks. Open-source contributor — authored multi-qr npm package and built Table-Tap, a live contactless ordering platform with RAG-powered analytics on GCP Cloud Run."""


def clean_locks():
    for lock_name in ['SingletonLock', 'SingletonSocket', 'SingletonCookie']:
        p = os.path.join(PROFILE_DIR, lock_name)
        if os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass


def _click_save(page):
    try:
        ok = page.evaluate("""() => {
          const btns=[...document.querySelectorAll('button')]
            .filter(b => b.offsetParent !== null && !b.disabled
                         && (b.textContent||'').trim().toLowerCase() === 'save');
          if(!btns.length) return false;
          btns[0].scrollIntoView({block:'center'});
          btns[0].click();
          return true;
        }""")
        if ok:
            page.wait_for_timeout(4000)
            return True, "saved"
        return False, "no visible enabled Save button"
    except Exception as e:
        return False, f"save failed: {str(e)[:90]}"


def update_headline(page, headline):
    """Update the resume headline field."""
    try:
        page.goto(PROFILE_URL, timeout=60000, wait_until="domcontentloaded")
        page.wait_for_timeout(7000)

        if "login" in page.url.lower():
            return False, "not logged in"

        # Find the headline edit button
        opened = page.evaluate("""() => {
          const heads = [...document.querySelectorAll('span,div,h2,h3,label')]
            .filter(e => (e.textContent||'').trim().startsWith('Resume headline'));
          if(!heads.length) return false;
          const card = heads[0].closest('div');
          const ed = [...card.querySelectorAll('span,em,a,button')]
            .filter(e => /edit/i.test(e.className||''));
          if(!ed.length) return false;
          ed[0].click(); return true;
        }""")
        if not opened:
            return False, "headline edit control not found"
        page.wait_for_timeout(3000)

        # Find textarea or input
        input_field = page.locator("textarea, input[type='text']").first
        if not input_field.count():
            return False, "headline input not found"

        input_field.click()
        input_field.fill("")
        page.wait_for_timeout(500)
        input_field.type(headline, delay=20)
        page.wait_for_timeout(1000)

        ok, detail = _click_save(page)
        if not ok:
            return False, detail
        page.wait_for_timeout(2000)
        return True, "headline updated"
    except Exception as e:
        return False, f"headline error: {str(e)[:110]}"


def update_profile_summary(page, summary):
    """Update the profile summary / about section."""
    try:
        page.goto(PROFILE_URL, timeout=60000, wait_until="domcontentloaded")
        page.wait_for_timeout(7000)

        if "login" in page.url.lower():
            return False, "not logged in"

        # Try to find profile summary section
        opened = page.evaluate("""() => {
          const labels = [...document.querySelectorAll('span,div,h2,h3,label')]
            .filter(e => {
              const t = (e.textContent||'').trim().toLowerCase();
              return t.includes('profile summary') || t.includes('about') || t.includes('career summary');
            });
          if(!labels.length) return false;
          const card = labels[0].closest('div');
          const ed = [...card.querySelectorAll('span,em,a,button')]
            .filter(e => /edit/i.test(e.className||''));
          if(!ed.length) return false;
          ed[0].click(); return true;
        }""")
        if not opened:
            return False, "profile summary edit not found"
        page.wait_for_timeout(3000)

        # Find textarea
        textarea = page.locator("textarea").first
        if not textarea.count():
            return False, "textarea not found"

        textarea.click()
        textarea.fill("")
        page.wait_for_timeout(500)
        textarea.type(summary, delay=10)
        page.wait_for_timeout(1000)

        ok, detail = _click_save(page)
        if not ok:
            return False, detail
        page.wait_for_timeout(2000)
        return True, "profile summary updated"
    except Exception as e:
        return False, f"summary error: {str(e)[:110]}"


def add_skills(page, skills):
    """Add skills to Key skills section."""
    done = []
    try:
        page.goto(PROFILE_URL, timeout=60000, wait_until="domcontentloaded")
        page.wait_for_timeout(7000)

        if "login" in page.url.lower():
            return done, "not logged in"

        opened = page.evaluate("""() => {
          const heads=[...document.querySelectorAll('span,div,h2,h3,label')]
            .filter(e=>(e.textContent||'').trim().startsWith('Key skills')
                       && e.children.length<6 && !e.closest('.quickLink'));
          if(!heads.length) return false;
          const card=heads[0].closest('div');
          const ed=[...card.querySelectorAll('span,em,a,button')]
            .filter(e=>/edit/i.test(e.className||''));
          if(!ed.length) return false;
          ed[0].click(); return true;
        }""")
        if not opened:
            return done, "skills edit control not found"
        page.wait_for_timeout(4000)

        box = page.locator('input#keySkillSugg, input[placeholder="Add skills"]').first
        if not box.count():
            return done, "skill input not found"

        for s in skills:
            box.click()
            box.fill('')
            box.type(s, delay=60)
            page.wait_for_timeout(1500)

            picked = page.evaluate("""(skill) => {
              const boxes=[...document.querySelectorAll(
                '[id*="sugDrp" i], [class*="sugg" i], [class*="Sbtn" i], ul[class*="sug" i]')];
              const items=boxes.flatMap(b=>[...b.querySelectorAll('li,div[role="option"]')])
                .filter(e=>e.offsetParent!==null && (e.textContent||'').trim());
              if(!items.length) return false;
              const t=skill.toLowerCase();
              const hit=items.find(e=>e.textContent.trim().toLowerCase()===t)
                     || items.find(e=>e.textContent.trim().toLowerCase().includes(t))
                     || items[0];
              hit.click(); return true;
            }""", s)
            if not picked:
                page.keyboard.press('ArrowDown')
                page.wait_for_timeout(400)
                page.keyboard.press('Enter')
            page.wait_for_timeout(800)
            done.append(s)

        ok, detail = _click_save(page)
        if not ok:
            return done, detail
        page.wait_for_timeout(2500)

        # Verify
        body = page.inner_text("body")
        landed = [s for s in done if s.lower() in body.lower()]
        return landed, f"saved {len(landed)} of {len(done)}"
    except Exception as e:
        return done, f"error: {str(e)[:110]}"


def upload_resume(page, resume_path):
    """Upload/replace the resume PDF."""
    if not os.path.exists(resume_path):
        return False, f"resume not found: {resume_path}"
    try:
        page.goto(PROFILE_URL, timeout=60000, wait_until="domcontentloaded")
        page.wait_for_timeout(7000)

        if "login" in page.url.lower():
            return False, "not logged in"

        # Look for resume upload section
        upload_found = page.evaluate("""() => {
          const labels = [...document.querySelectorAll('span,div,h2,h3,label')]
            .filter(e => {
              const t = (e.textContent||'').trim().toLowerCase();
              return t.includes('resume') || t.includes('upload resume') || t.includes('attachment');
            });
          if(!labels.length) return false;
          const card = labels[0].closest('div');
          const fileInput = card.querySelector('input[type="file"]');
          if(fileInput) { fileInput.id = '_resume_upload'; return true; }
          return false;
        }""")
        if not upload_found:
            # Try direct file input search
            has_input = page.evaluate("""() => {
              const fi = document.querySelector('input[type="file"]');
              if(fi) { fi.id = '_resume_upload'; return true; }
              return false;
            }""")
            if not has_input:
                return False, "resume upload input not found"

        file_input = page.locator('input#_resume_upload')
        file_input.set_input_files(resume_path)
        page.wait_for_timeout(5000)

        # Check for upload success message
        body = page.inner_text("body")
        if "upload" in body.lower() and ("success" in body.lower() or "resume" in body.lower()):
            return True, "resume uploaded"
        return True, "resume upload triggered"
    except Exception as e:
        return False, f"upload error: {str(e)[:110]}"


def refresh_profile(page):
    """Re-save to bump freshness."""
    try:
        page.goto(PROFILE_URL, timeout=60000, wait_until="domcontentloaded")
        page.wait_for_timeout(7000)
        edit = page.locator('em.edit, span.edit, [class*="edit"]').first
        if not edit.count():
            return False, "edit control not found"
        edit.click()
        page.wait_for_timeout(3000)
        ok, detail = _click_save(page)
        if not ok:
            try:
                page.keyboard.press('Escape')
            except Exception:
                pass
            return False, detail
        return True, "freshness bumped"
    except Exception as e:
        return False, f"error: {str(e)[:110]}"


def run():
    clean_locks()
    results = {}

    try:
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=PROFILE_DIR, channel="chrome", headless=True,
                locale="en-IN", timezone_id="Asia/Kolkata",
                viewport={"width": 1440, "height": 900},
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
            page = ctx.new_page()

            # Step 1: Update headline
            print("=== Step 1: Updating headline ===", file=sys.stderr)
            ok, detail = update_headline(page, NEW_HEADLINE)
            results["headline"] = {"ok": ok, "detail": detail}
            print(f"Headline: {detail}", file=sys.stderr)

            # Step 2: Add missing skills
            print("\n=== Step 2: Adding missing skills ===", file=sys.stderr)
            added, detail = add_skills(page, SKILLS_TO_ADD)
            results["skills"] = {"added": added, "detail": detail}
            print(f"Skills: {detail}", file=sys.stderr)

            # Step 3: Update profile summary
            print("\n=== Step 3: Updating profile summary ===", file=sys.stderr)
            ok, detail = update_profile_summary(page, PROFILE_SUMMARY)
            results["profile_summary"] = {"ok": ok, "detail": detail}
            print(f"Summary: {detail}", file=sys.stderr)

            # Step 4: Upload resume
            print("\n=== Step 4: Uploading resume ===", file=sys.stderr)
            ok, detail = upload_resume(page, BEST_RESUME)
            results["resume_upload"] = {"ok": ok, "detail": detail}
            print(f"Resume: {detail}", file=sys.stderr)

            # Step 5: Refresh freshness
            print("\n=== Step 5: Refreshing profile ===", file=sys.stderr)
            ok, detail = refresh_profile(page)
            results["refresh"] = {"ok": ok, "detail": detail}
            print(f"Refresh: {detail}", file=sys.stderr)

            ctx.close()
    except Exception as e:
        results["error"] = str(e)

    return results


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, indent=2))