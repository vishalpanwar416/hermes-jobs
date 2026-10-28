#!/usr/bin/env python3
"""Instahyre Auto-Apply - v3: uses correct apply endpoint."""
import asyncio, os, json, sys, subprocess, time
from pathlib import Path

AUTOMATION_PROFILE = os.path.expanduser("~/.hermes/data/chrome_automation_profile")
os.makedirs(AUTOMATION_PROFILE, exist_ok=True)
for f in ["SingletonLock", "SingletonSocket", "SingletonCookie"]:
    p = os.path.join(AUTOMATION_PROFILE, f)
    if os.path.exists(p): os.remove(p)

env_path = Path.home() / ".hermes" / ".env"
EMAIL = PASSWORD = None
for line in env_path.read_text().splitlines():
    if line.startswith("INSTAHYRE_EMAIL="):
        EMAIL = line.split("=", 1)[1].strip().strip('"').strip("'")
    elif line.startswith("INSTAHYRE_PASSWORD="):
        PASSWORD = line.split("=", 1)[1].strip().strip('"').strip("'")

MATCH_KW = ["golang", "backend", "devops", "sre", "kubernetes", "full stack",
            "fullstack", "python", "gcp", "cloud", "sde", "software engineer",
            "infrastructure", "node", "nodejs", "mern", "java", "distributed", "platform"]
EXCLUDE_KW = ["senior", "lead", "manager", "principal", "staff", "architect",
              "frontend", "front-end", "ui", "ux", "react", "angular",
              "ios", "android", "qa", "test", "data scientist", "ml engineer",
              "vue", "css", "html", "sdet", "analyst", "director", "cto"]

def matches(t):
    t = t.lower()
    if not any(kw in t for kw in MATCH_KW): return False
    if any(kw in t for kw in EXCLUDE_KW): return False
    return True

def start_xvfb():
    for n in range(91, 110):
        if os.path.exists(f"/tmp/.X{n}-lock"): continue
        proc = subprocess.Popen(["Xvfb", f":{n}", "-screen", "0", "1440x900x24"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.5)
        if proc.poll() is None: return proc, f":{n}"
    raise RuntimeError("no Xvfb display")

async def main():
    xvfb, d = start_xvfb()
    os.environ["DISPLAY"] = d
    result = {"status": "ok", "jobs_applied": 0, "jobs_found": 0}

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        xvfb.terminate()
        print(json.dumps({"error": "playwright not installed"}))
        return

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=AUTOMATION_PROFILE, channel="chrome",
            headless=False, args=["--no-sandbox", "--disable-gpu"],
        )
        page = await ctx.new_page()
        page.set_default_timeout(45000)

        # Go to opportunities page
        await page.goto("https://www.instahyre.com/candidate/opportunities/",
                        wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(6)

        if "login" in page.url.lower():
            await page.locator('input[name="email"]').first.fill(EMAIL)
            await page.locator('input[type="password"]').first.fill(PASSWORD)
            await page.locator('button[type="submit"]').first.click()
            await asyncio.sleep(6)

        # Wait for Angular
        import re
        for _ in range(30):
            body = await page.locator("body").inner_text()
            if re.search(r'Interested\s*\((\d+)\)', body):
                break
            await asyncio.sleep(2)

        # Fetch interested jobs
        jobs = await page.evaluate("""async (limit) => {
            const r = await fetch('/api/v1/candidate_opportunities/candidate_matching?interest_facet=1&limit='+limit+'&offset=0', {credentials: 'include'});
            const j = await r.json();
            return (j.objects || []).map(o => ({
                id: o.id, job_id: ((o.job||{}).id||''),
                title: (o.job||{}).title||'',
                company: (o.employer||{}).name||'',
                score: o.score,
                applied: o.interview_status >= 2,
            }));
        }""", 200)

        result["total"] = len(jobs)
        target = [j for j in jobs if matches(j.get('title','')) and not j.get('applied')]
        result["jobs_found"] = len(target)

        # Apply via POST to /apply/ endpoint
        for job in target[:50]:
            post = await page.evaluate("""async (jobId) => {
                try {
                    const r = await fetch('/api/v1/candidate_opportunities/candidate_matching/apply/', {
                        method: 'POST',
                        credentials: 'include',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({"is_interested": true, "job_id": jobId})
                    });
                    const txt = await r.text();
                    return {status: r.status, ok: r.ok, body: txt.substring(0, 100)};
                } catch(e) { return {error: e.message}; }
            }""", job['id'])

            if post.get('ok'):
                result["jobs_applied"] += 1
                print(f"  ✅ {job['title']} @ {job['company']}")

        await ctx.close()

    xvfb.terminate()
    try: xvfb.wait(timeout=3)
    except: xvfb.kill()

    print(f"\nApplied: {result['jobs_applied']}/{result['jobs_found']}")
    print(json.dumps(result))

asyncio.run(main())