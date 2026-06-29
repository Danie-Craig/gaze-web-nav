#!/usr/bin/env python3
"""
Regenerate the logged-in storage state the recorder loads (./shopping_state.json).

Why this exists: cookies in the storage state expire, and they're scoped to a
specific host. This logs in at the SAME url the recorder uses, so the saved
cookies match the domain the recorder will navigate to. Run it from
data_collection/ (so ./shopping_state.json lands next to record.py).

It auto-fills the Magento login form (best effort). If the selectors ever miss,
the browser stays open -- just sign in by hand, then press Enter.

  python make_login_state.py
"""
from playwright.sync_api import sync_playwright

# Must match the recorder's TASK_URL exactly (same host => cookies apply).
BASE = "http://liralabwidowx-alienware-aurora-r16.tail4d611e.ts.net:8082"
EMAIL = "emma.lopez@gmail.com"
PASSWORD = "Password.123"
OUT = "./shopping_state.json"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    ctx = browser.new_context()
    page = ctx.new_page()
    page.goto(f"{BASE}/customer/account/login/", wait_until="domcontentloaded")

    # Best-effort auto-login (standard Magento Luma selectors).
    try:
        page.fill("#email", EMAIL, timeout=5000)
        page.fill("#pass", PASSWORD, timeout=5000)
        page.click("#send2", timeout=5000)
        page.wait_for_load_state("networkidle", timeout=20000)
    except Exception as e:
        print(f"(auto-fill didn't complete: {e})")
        print("Sign in by hand in the browser window.")

    print("\n>>> Check the browser: top-right should show 'Sign Out' (= you're logged in).")
    print(">>> If it still shows 'Sign In', log in manually now (emma.lopez@gmail.com / Password.123).")
    input(">>> Then press Enter here to save the login state... ")

    ctx.storage_state(path=OUT)
    print(f"Saved login state to {OUT}")
    browser.close()
