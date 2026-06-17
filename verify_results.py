"""Results / eval-comparison E2E against REAL eval data.

This is the core product the earlier suites skipped: selecting a real eval run
and confirming the comparison view, run rail, sample drill-down, deep-link, and
report button all render real content. Run against `eval-mcp view` pointed at
real user storage (~/.eval-mcp/users), so no Bedrock spend.

  1. /results lists real runs in the rail
  2. selecting a run sets ?group=<id> and renders scores
  3. deep-link to /results?group=<id> hard-loads the comparison
  4. sample drill-down opens a detail panel
  5. Report download button is present
  6. back/forward between "no selection" and a selected run
  7. no JS exceptions throughout
"""

import re
import sys
from playwright.sync_api import sync_playwright

BASE = "http://localhost:4001"
failures: list[str] = []
page_errors: list[str] = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


SCORE_RE = re.compile(r"\d+%|\d\.\d{2,}|mean", re.I)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.on("pageerror", lambda e: page_errors.append(str(e)))

    # 1. landing lists real runs
    page.goto(BASE + "/results", wait_until="networkidle")
    page.wait_for_timeout(2000)
    rail = page.locator("aside button")
    check("1. run rail lists real runs", rail.count() > 0, f"rail count={rail.count()}")
    check("1. shows 'no run selected' prompt",
          "No run selected" in page.inner_text("body") or "Pick" in page.inner_text("body"))

    # 2. select first run → ?group= + scores render
    rail.first.click()
    page.wait_for_timeout(2500)
    check("2. selecting a run sets ?group=", "group=" in page.url, f"url={page.url}")
    selected_url = page.url
    check("2. comparison renders scores", bool(SCORE_RE.search(page.inner_text("body"))))

    # 3. deep-link hard-load of that same run
    page.goto(selected_url, wait_until="networkidle")
    page.wait_for_timeout(2500)
    check("3. deep-link hard-load renders scores",
          "group=" in page.url and bool(SCORE_RE.search(page.inner_text("body"))))

    # 4. sample drill-down
    cells = page.locator("[class*=grid] button, tbody button, [class*=sample] button")
    if cells.count():
        before = len(page.inner_text("body"))
        cells.first.click()
        page.wait_for_timeout(1500)
        check("4. sample drill-down opens detail", len(page.inner_text("body")) != before)
    else:
        check("4. sample drill-down opens detail", False, "no sample cells found")

    # 5. report button
    check("5. Report button present",
          page.locator("button", has_text="Report").count() > 0)

    # 6. back/forward: go back to plain /results, forward to the run
    page.goto(BASE + "/results", wait_until="networkidle")
    page.wait_for_timeout(1000)
    page.locator("aside button").first.click()
    page.wait_for_timeout(2000)
    run_url = page.url
    page.go_back()
    page.wait_for_timeout(1200)
    check("6. back leaves the selected run", page.url != run_url, f"url={page.url}")
    page.go_forward()
    page.wait_for_timeout(2000)
    check("6. forward returns to the run",
          page.url == run_url and bool(SCORE_RE.search(page.inner_text("body"))),
          f"url={page.url}")

    check("7. no uncaught JS exceptions", len(page_errors) == 0, f"{page_errors[:3]}")
    browser.close()

print()
if failures:
    print(f"FAILED ({len(failures)}): {failures}")
    sys.exit(1)
print("ALL RESULTS CHECKS PASSED")
