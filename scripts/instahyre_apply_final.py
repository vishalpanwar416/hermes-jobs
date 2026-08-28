#!/usr/bin/env python3
"""
Instahyre Auto-Apply - Robust version.
"""
import asyncio, os, json, sys
from pathlib import Path
from playwright.async_api import async_playwright

env_path = Path.home() / ".hermes" / ".env"
email = password = None
for line in env_path.read_text().splitlines():
    if line.startswith("INSTAHYRE_EMAIL="):
        email = line.split("=", 1)[1].strip().strip('"').strip("'")
    elif line.startswith("INSTAHYRE_PASSWORD="):
        password = line.split("=", 1)[1].strip().strip('"').strip("'")

async def main():
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=os.path.expanduser("~/.config/google-chrome"),
            headless=False,
            args=["--no-sandbox", "--disable-gpu"],
        )
        page = await context.new_page()

        try:
            # === LOGIN ===
            print("Logging in...")
            await page.goto("https://www.instahyre.com/login/", timeout=30000)
            await asyncio.sleep(5)
            for i in range(60):
                if "login" not in page.url.lower():
                    break
                try:
                    if await page.locator('input[name="email"]').first.is_visible(timeout=1000):
                        break
                except: pass
                await asyncio.sleep(2)
            if "login" in page.url.lower():
                await page.locator('input[name="email"]').first.fill(email, timeout=10000)
                await page.locator('input[type="password"]').first.fill(password, timeout=10000)
                await page.locator('button[type="submit"]').first.click()
                await asyncio.sleep(5)
            print(f"✅ Logged in")

            # === GET OPPORTUNITIES ===
            await page.goto("https://www.instahyre.com/candidate/opportunities/?matching=", timeout=20000)
            await asyncio.sleep(5)

            # Get all job info in one JS call
            jobs_info = await page.evaluate("""() => {
                const viewBtns = Array.from(document.querySelectorAll('button'));
                const viewOnly = viewBtns.filter(b => b.innerText.trim() === 'View \u00bb');
                return viewOnly.slice(0, 30).map((btn, idx) => {
                    // Walk up to find the card container
                    let el = btn.parentElement;
                    for (let i = 0; i < 5; i++) {
                        if (el?.querySelector('a[href*="/job/"]')) break;
                        el = el?.parentElement;
                        if (!el) break;
                    }
                    return {
                        index: idx,
                        text: el ? el.innerText.substring(0, 200) : btn.innerText,
                        hasApplyPage: !!(el?.querySelector('a[href*="/job/"]'))
                    };
                });
            }""")

            print(f"Found {len(jobs_info)} jobs")

            # Filter matching jobs
            MATCH_KW = ["go", "golang", "backend", "platform", "devops", "sre", "kubernetes",
                        "k8s", "full stack", "fullstack", "python", "gcp", "cloud",
                        "sde", "software", "engineer", "infrastructure"]
            EXCLUDE_KW = ["senior", "lead", "manager", "principal", "staff", "architect",
                          "frontend", "front-end", "ui", "ux", "react", "angular",
                          "ios", "android", "qa", "test", "data scientist", "ml engineer"]

            matching = []
            for job in jobs_info:
                t = job["text"].lower()
                if any(kw in t for kw in MATCH_KW) and not any(kw in t for kw in EXCLUDE_KW):
                    matching.append(job)
                    print(f"  ✅ {job['text'][:80]}")

            print(f"\nMatching jobs: {len(matching)}")

            # Apply to matching jobs
            applied = 0
            for job in matching:
                idx = job["index"]
                print(f"\nApplying to job {idx+1}...")

                # Click the View button at this index
                view_btns = page.locator('button:has-text("View")')
                await view_btns.nth(idx).click()
                await asyncio.sleep(5)

                # Look for Apply button
                apply_btn = None
                for sel in ['button:has-text("Apply")', 'a:has-text("Apply")', 'button[class*="apply"]', 'a[href*="apply"]', 'input[value*="Apply"]']:
                    try:
                        btn = page.locator(sel).first
                        if await btn.is_visible(timeout=2000):
                            apply_btn = btn
                            break
                    except: continue

                if apply_btn:
                    # Check if it's disabled
                    try:
                        disabled = await apply_btn.is_disabled()
                    except:
                        disabled = False
                    btn_text = await apply_btn.inner_text()
                    print(f"  Apply button: '{btn_text[:40]}' disabled={disabled}")
                    if not disabled:
                        await apply_btn.click()
                        await asyncio.sleep(3)
                        applied += 1
                        print(f"  ✅ Applied! ({applied})")
                    else:
                        print(f"  ❌ Apply button disabled")
                else:
                    print(f"  ❌ No Apply button found")

                # Go back
                await page.go_back()
                await asyncio.sleep(3)

            print(json.dumps({
                "status": "success",
                "jobs_found": len(jobs_info),
                "jobs_matched": len(matching),
                "jobs_applied": applied
            }))

        except Exception as e:
            print(json.dumps({"error": str(e)}))
        finally:
            await asyncio.sleep(3)
            await context.close()

if __name__ == "__main__":
    asyncio.run(main())