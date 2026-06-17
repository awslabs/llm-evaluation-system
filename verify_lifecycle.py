"""Session-lifecycle E2E — the migration-sensitive navigation/URL flows.

These exercise the code the Vite migration actually changed: react-router
useSearchParams (?session=), the autoLoadedRef guard in Chat.tsx, and the
setSearchParams-based "+ New chat" URL clear in ChatInterface.tsx. Run against
the full stack (live Bedrock) at single-origin :4001.

Covered:
  1. send in a fresh chat → assistant replies, message persists
  2. open a session from History → ?session= in URL + correct transcript
  3. reload on /chat?session=X → deep link restores that exact session
  4. tab-switch Chat→Results→Chat → conversation survives
  5. "+ New chat" → clears ?session= AND empties the transcript (autoLoadedRef
     race: must NOT snap back to the previous conversation)
  6. switch between two different sessions → no message bleed
  7. browser back/forward across sessions

Marker words (ALPHA/BRAVO/...) make each conversation uniquely identifiable so
we can assert the RIGHT transcript is shown, not just "some text".
"""

import sys
import uuid
from playwright.sync_api import sync_playwright, expect

BASE = "http://localhost:4001"
failures: list[str] = []
page_errors: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def wait_stream_done(page, timeout=40000):
    """Streaming is finished when the Send button is back (Stop gone)."""
    expect(page.locator("button", has_text="Send")).to_be_visible(timeout=timeout)


def send(page, text: str):
    page.locator("textarea").first.fill(text)
    page.locator("button", has_text="Send").first.click()
    wait_stream_done(page)
    page.wait_for_timeout(800)


def session_rows(page):
    """The History session-list buttons (li that contain an h3 title),
    NOT the nav <ul>."""
    return page.locator("li", has=page.locator("h3")).locator("button")


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.on("pageerror", lambda e: page_errors.append(str(e)))

    # Unique markers so we can tell conversations apart.
    m1 = "ALPHA" + uuid.uuid4().hex[:6].upper()
    m2 = "BRAVO" + uuid.uuid4().hex[:6].upper()

    # --- 1. fresh chat send ---
    page.goto(BASE + "/chat", wait_until="networkidle")
    page.wait_for_timeout(1500)
    send(page, f"Reply with exactly this token and nothing else: {m1}")
    body = page.inner_text("body")
    check("1. fresh send: user msg present", m1 in body)
    check("1. fresh send: assistant replied", body.count(m1) >= 2,
          f"marker count={body.count(m1)}")

    # --- 2. open that session from History ---
    page.goto(BASE + "/history", wait_until="networkidle")
    page.wait_for_timeout(1500)
    # the just-created session should be the top row (most recent)
    top = session_rows(page).first
    top_title = top.locator("h3").inner_text()
    top.click()
    page.wait_for_timeout(2000)
    check("2. history-open: URL has ?session=", "session=" in page.url,
          f"url={page.url}")
    sess1_url = page.url
    check("2. history-open: correct transcript", m1 in page.inner_text("body"))

    # --- 3. reload restores the session via deep link ---
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(2500)
    check("3. reload: URL preserved", page.url == sess1_url, f"url={page.url}")
    check("3. reload: transcript restored", m1 in page.inner_text("body"))

    # --- 4. tab-switch Chat -> Results -> Chat, convo survives ---
    page.locator("nav button", has_text="Results").first.click()
    page.wait_for_timeout(800)
    check("4. tab-switch: on /results", page.url.endswith("/results") or "results" in page.url)
    page.locator("nav button", has_text="Chat").first.click()
    page.wait_for_timeout(2000)
    check("4. tab-switch: back on chat with convo", m1 in page.inner_text("body"),
          f"url={page.url}")

    # --- 5. "+ New chat" clears session and does NOT snap back ---
    new_btn = page.locator("button", has_text="New chat")
    if new_btn.count():
        new_btn.first.click()
        page.wait_for_timeout(2000)
        check("5. new-chat: ?session= cleared", "session=" not in page.url,
              f"url={page.url}")
        # The old conversation's marker must be GONE (autoLoadedRef race).
        check("5. new-chat: old transcript cleared (no snap-back)",
              m1 not in page.inner_text("body"),
              "old conversation reappeared — autoLoadedRef regression")
    else:
        check("5. new-chat: button present", False, "no New chat button")
        check("5. new-chat: old transcript cleared (no snap-back)", False, "n/a")

    # --- 6. create a 2nd convo, switch between the two, no bleed ---
    send(page, f"Reply with exactly this token and nothing else: {m2}")
    body2 = page.inner_text("body")
    check("6. second convo: has m2", m2 in body2)
    check("6. second convo: does NOT show m1", m1 not in body2,
          "first conversation bled into second")

    # open session 1 again from history, confirm m1 (not m2)
    page.goto(BASE + "/history", wait_until="networkidle")
    page.wait_for_timeout(1500)
    # find the row whose title matches m1's conversation
    rows = session_rows(page)
    n = rows.count()
    opened = False
    for i in range(min(n, 10)):
        r = rows.nth(i)
        if m1 in r.inner_text():
            r.click()
            page.wait_for_timeout(2000)
            opened = True
            break
    if opened:
        b = page.inner_text("body")
        check("6. switch back to convo1: shows m1", m1 in b)
        check("6. switch back to convo1: no m2 bleed", m2 not in b,
              "second conversation bled into first")
    else:
        check("6. switch back to convo1: shows m1", False, "couldn't find convo1 row")
        check("6. switch back to convo1: no m2 bleed", False, "n/a")

    # --- 7. browser back/forward across sessions ---
    url_convo1 = page.url
    page.go_back()
    page.wait_for_timeout(1500)
    check("7. back navigates away from convo1", page.url != url_convo1,
          f"url={page.url}")
    page.go_forward()
    page.wait_for_timeout(2000)
    check("7. forward returns to convo1", page.url == url_convo1 and m1 in page.inner_text("body"),
          f"url={page.url}")

    check("no uncaught JS exceptions", len(page_errors) == 0,
          f"{len(page_errors)}: {page_errors[:3]}")

    browser.close()

print()
if failures:
    print(f"FAILED ({len(failures)}): {failures}")
    sys.exit(1)
print("ALL LIFECYCLE CHECKS PASSED")
