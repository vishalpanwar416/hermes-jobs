import os
import json
import time
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

def comment_on_posts():
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    
    # Use existing chrome profile if available or standard
    user_data_dir = os.path.expanduser("~/.config/google-chrome")
    if os.path.exists(user_data_dir):
        options.add_argument(f"--user-data-dir={user_data_dir}")
        options.add_argument("--profile-directory=Default")

    driver = webdriver.Chrome(options=options)
    try:
        # Target posts from our candidate list: Harshil Mathur (RAY agentic) & Himanshu Wasnik (DevOps/SRE interview)
        urls = [
            "https://www.linkedin.com/posts/harshilmathur_first-we-made-the-razorpay-dashboard-agentic-ugcPost-7503443691185836033-prIo/",
            "https://www.linkedin.com/posts/himanshu-wasnik-7b8198230_recently-attended-a-devopssre-interview-share-7502735444938407936-2zwm/"
        ]
        
        comments = [
            "Skipping the dashboard entirely for WhatsApp is brilliant. The hard part is state management when users ask complex multi-step queries—how are you handling context persistence across sessions?",
            "Production troubleshooting questions like these separate textbook knowledge from actual experience. Handling a Pod stuck in Pending or a sudden 100% CPU spike teaches you more about Kubernetes internals than any cert."
        ]

        for url, comment_text in zip(urls, comments):
            print(f"Navigating to {url}")
            driver.get(url)
            time.sleep(5)
            
            try:
                # Find comment button or comment box
                comment_box = driver.find_element(By.CSS_SELECTOR, "div.ql-editor, div[contenteditable='true']")
                driver.execute_script("arguments[0].scrollIntoView(true);", comment_box)
                time.sleep(1)
                comment_box.click()
                comment_box.send_keys(comment_text)
                time.sleep(2)
                
                # Find post button
                post_button = driver.find_element(By.CSS_SELECTOR, "button.comments-comment-box__submit-button, button.artdeco-button--primary")
                post_button.click()
                print(f"Successfully posted comment on {url}")
                time.sleep(3)
            except Exception as e:
                print(f"Failed to post on {url}: {e}")
                
    finally:
        driver.quit()

if __name__ == "__main__":
    comment_on_posts()
