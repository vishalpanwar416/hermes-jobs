"""One-time interactive Naukri login for the automation Chrome profile.

Run this from a desktop session (needs a display):

    ~/Development/Aarambh/hermes-agent/.venv/bin/python ~/.hermes/scripts/naukri_login.py

A Chrome window opens on the shared automation profile at the Naukri login
page. Log in manually (password or OTP). The script watches for the logged-in
session and exits on its own; cookies persist in the profile, which is what
naukri_auto_apply.py (the hourly cron) uses headlessly afterwards.
"""

import os
import sys
import time

from playwright.sync_api import sync_playwright

PROFILE_DIR = os.path.expanduser("~/.hermes/data/chrome_automation_profile")
TIMEOUT_MIN = 10


def main():
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            channel="chrome",
            headless=False,
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            viewport={"width": 1440, "height": 900},
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        page = context.new_page()
        page.goto("https://www.naukri.com/nlogin/login", timeout=60000)
        print(f"Log in to Naukri in the Chrome window (waiting up to {TIMEOUT_MIN} min)...")

        deadline = time.time() + TIMEOUT_MIN * 60
        while time.time() < deadline:
            time.sleep(5)
            try:
                cookies = {c["name"] for c in context.cookies("https://www.naukri.com")}
                # nauk_at / nauk_rt are Naukri's auth/refresh tokens
                if "nauk_at" in cookies or "nauk_rt" in cookies:
                    print("✓ Logged in — session cookies saved to the automation profile.")
                    print("Verifying recommended-jobs page loads...")
                    page.goto("https://www.naukri.com/mnjuser/recommendedjobs", timeout=45000)
                    time.sleep(5)
                    print(f"Landed on: {page.url} | title: {page.title()}")
                    context.close()
                    return 0
            except Exception:
                pass
        print("✗ Timed out waiting for login.")
        context.close()
        return 1


if __name__ == "__main__":
    sys.exit(main())
