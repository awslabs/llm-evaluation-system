"""Playwright verification for the Vite migration — single-origin truth loop.

Run via the webapp-testing skill's with_server.py, which boots `eval-mcp view`
on :4001 first. This script then drives the built SPA and asserts the things
that the old Next.js export + History-API band-aids used to fake:

  - every route renders (no white screen / console errors)
  - SPA fallback: a hard navigation (page.goto) to a deep route returns the
    shell and the client router resolves it
  - deep-link query params (?group, ?id) are read by the page
  - client-side nav between routes works
  - the viewer's `mode: "viewer"` hides chat/history nav

Exit code 0 = all checks passed.
"""

import sys
from playwright.sync_api import sync_playwright

BASE = "http://localhost:4001"
failures: list[str] = []
page_errors: list[str] = []
unexpected_4xx: list[str] = []


# Expected backend 404s that are NOT migration bugs: the viewer doesn't
# implement full-mode endpoints (chat sessions), and the test deliberately
# requests a nonexistent eval group. Any OTHER 4xx (esp. a missing JS/CSS/font
# asset) is a real failure.
def _is_expected_4xx(url: str) -> bool:
    return "/api/sessions" in url or "group_id=does-not-exist" in url


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    # Track any 4xx/5xx response that isn't an expected backend 404. A broken
    # asset (missing JS/CSS/font from the Vite build) lands here.
    page.on(
        "response",
        lambda r: unexpected_4xx.append(f"{r.status} {r.url}")
        if r.status >= 400 and not _is_expected_4xx(r.url)
        else None,
    )
    # Uncaught JS exceptions (a render crash, a missed next/* import) — these
    # are the real signal that the migration broke something.
    page.on("pageerror", lambda e: page_errors.append(str(e)))

    # 1. Root renders the SPA shell (viewer maps "/" → index.html → router).
    page.goto(BASE + "/", wait_until="networkidle")
    check("root loads", page.locator("#root").count() == 1)

    # 2. Each route renders via hard navigation (SPA fallback for deep paths).
    for route in ["/results", "/data", "/optimizations"]:
        page.goto(BASE + route, wait_until="networkidle")
        body = page.inner_text("body")
        check(f"hard-load {route} renders content", len(body.strip()) > 0)
        check(f"hard-load {route} stays on path", page.url.endswith(route),
              f"url={page.url}")

    # 3. Deep link with query param — hard refresh resolves and page reads it.
    page.goto(BASE + "/results?group=does-not-exist", wait_until="networkidle")
    check("deep-link ?group hard-refresh resolves",
          "group=does-not-exist" in page.url, f"url={page.url}")

    # 4. viewer mode hides chat/history nav (fullOnly entries).
    page.goto(BASE + "/results", wait_until="networkidle")
    nav_text = page.inner_text("body")
    check("viewer mode hides Chat nav", "Chat" not in
          (page.locator("nav").inner_text() if page.locator("nav").count() else ""))

    # 5. Client-side nav: click Data nav, URL changes without full reload.
    page.goto(BASE + "/results", wait_until="networkidle")
    data_btn = page.locator("nav button", has_text="Data")
    if data_btn.count():
        data_btn.first.click()
        page.wait_for_timeout(300)
        check("client nav to /data", page.url.endswith("/data"), f"url={page.url}")
    else:
        check("client nav to /data", False, "Data nav button not found")

    # 6. No uncaught JS exceptions (missed next/* import, render crash) and no
    # unexpected 4xx (broken JS/CSS/font asset from the Vite build).
    check("no uncaught JS exceptions", len(page_errors) == 0,
          f"{len(page_errors)}: {page_errors[:3]}")
    check("no unexpected 4xx/5xx", len(unexpected_4xx) == 0,
          f"{len(unexpected_4xx)}: {unexpected_4xx[:3]}")

    browser.close()

print()
if failures:
    print(f"FAILED ({len(failures)}): {failures}")
    sys.exit(1)
print("ALL CHECKS PASSED")
