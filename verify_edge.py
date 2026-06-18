"""Edge-case + data-page E2E. Stress the interaction states and the Data CRUD
surface that the migration touched (all client components, react-router).

  1. empty/whitespace input → Send stays disabled (canSend guard)
  2. send while streaming is impossible (Send replaced by Stop)
  3. double-clicking Stop doesn't error (isCancelling guard)
  4. Data page: all three sub-tabs switch without error
  5. Data page: open a dataset detail (if any) renders rows
  6. deep-link to /data and /optimizations hard-loads cleanly
"""

import sys
from playwright.sync_api import sync_playwright, expect

BASE = "http://localhost:4001"
failures: list[str] = []
page_errors: list[str] = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.on("pageerror", lambda e: page_errors.append(str(e)))

    # --- 1. empty input keeps Send disabled ---
    page.goto(BASE + "/chat", wait_until="networkidle")
    page.wait_for_timeout(1500)
    send_btn = page.locator("button", has_text="Send").first
    check("1. empty input: Send disabled", send_btn.is_disabled())
    page.locator("textarea").first.fill("   ")  # whitespace only
    page.wait_for_timeout(200)
    check("1. whitespace input: Send still disabled", send_btn.is_disabled())

    # --- 2. while streaming, Send is replaced by Stop (can't double-send) ---
    page.locator("textarea").first.fill("Write a long 400-word paragraph about clouds.")
    page.locator("button", has_text="Send").first.click()
    expect(page.locator("button", has_text="Stop")).to_be_visible(timeout=15000)
    check("2. streaming: Send gone, Stop shown",
          page.locator("button", has_text="Send").count() == 0
          and page.locator("button", has_text="Stop").count() >= 1)

    # --- 3. double-click Stop doesn't throw / breaks nothing ---
    page.wait_for_timeout(1200)
    stop = page.locator("button", has_text="Stop").first
    stop.click()
    # immediately click again (button may be disabled/"Stopping…") — must not error
    try:
        page.locator("button", has_text="Stopping").first.click(timeout=1500)
    except Exception:
        pass  # disabled/gone is fine — the point is no crash
    expect(page.locator("button", has_text="Send")).to_be_visible(timeout=15000)
    check("3. double-stop: recovered to Send", True)

    # --- 4. Data page sub-tabs ---
    page.goto(BASE + "/data", wait_until="networkidle")
    page.wait_for_timeout(1500)
    tabs_ok = True
    for tab in ["Documents", "Judges", "Datasets"]:
        btn = page.locator("button", has_text=tab)
        if btn.count():
            btn.first.click()
            page.wait_for_timeout(700)
            if len(page.inner_text("body").strip()) == 0:
                tabs_ok = False
    check("4. data: all sub-tabs switch cleanly", tabs_ok)

    # --- 5. open a dataset detail if one exists ---
    page.locator("button", has_text="Datasets").first.click()
    page.wait_for_timeout(1000)
    # Dataset rows are "<li><button>…N SAMPLES…</button></li>" in the left rail.
    ds_rows = page.locator("li button").filter(has_text="SAMPLES")
    if ds_rows.count():
        before = page.inner_text("body")
        ds_rows.first.click()
        page.wait_for_timeout(1500)
        after = page.inner_text("body")
        # detail panel should add content (e.g. question/answer rows) — assert
        # the view changed and didn't blow up.
        check("5. dataset detail opens", len(after) > 300 and after != before,
              f"len before={len(before)} after={len(after)}")
    else:
        check("5. dataset detail opens", False, "no dataset rows found")

    # --- 6. deep-link hard loads ---
    for r in ["/data", "/optimizations"]:
        page.goto(BASE + r, wait_until="networkidle")
        page.wait_for_timeout(600)
        check(f"6. deep-load {r}", page.url.endswith(r) and len(page.inner_text("body").strip()) > 0)

    check("no uncaught JS exceptions", len(page_errors) == 0, f"{page_errors[:3]}")
    browser.close()

print()
if failures:
    print(f"FAILED ({len(failures)}): {failures}")
    sys.exit(1)
print("ALL EDGE CHECKS PASSED")
