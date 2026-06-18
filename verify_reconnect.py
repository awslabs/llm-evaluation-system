"""Reconnect-on-refresh E2E — the orphaned-stream fix.

Before this fix, refreshing the page mid-response left the running eval/stream
orphaned: the backend kept working but the UI showed a frozen/blank state.
Now Chat.tsx calls reconnectIfRunning() on mount, which probes
GET /api/chat/status/{id} and reattaches to the live SSE stream.

Also covers the companion fix: a fresh chat gets ?session= in the URL after its
first message, so a refresh has something to restore.

Needs live Bedrock. Exit 0 = passed.
"""

import sys
from playwright.sync_api import sync_playwright, expect

BASE = "http://localhost:4001"
failures: list[str] = []
page_errors: list[str] = []
status_calls: list[str] = []
reconnect_posts: list[str] = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1400, "height": 900})
    page.on("pageerror", lambda e: page_errors.append(str(e)))
    page.on("request", lambda r: status_calls.append(r.url) if "chat/status" in r.url else None)
    page.on("request", lambda r: reconnect_posts.append(r.url)
            if r.method == "POST" and "chat/message" in r.url else None)

    page.goto(BASE + "/chat", wait_until="networkidle")
    page.wait_for_timeout(1500)
    nb = page.locator("button", has_text="New chat")
    if nb.count():
        nb.first.click()
        page.wait_for_timeout(800)

    # --- Fix D: fresh chat syncs ?session= DURING streaming (not just after
    # completion). This is the "refresh mid-answer on a brand-new chat loses
    # the response" bug: without an early URL sync there's nothing for the
    # mount-time reconnect to latch onto. Assert the param appears while the
    # answer is still streaming. ---
    page.locator("textarea").first.fill(
        "Write a long 400-word essay about the ocean. Take your time.")
    page.locator("button", has_text="Send").first.click()
    expect(page.locator("button", has_text="Stop")).to_be_visible(timeout=15000)
    page.wait_for_timeout(1500)  # mid-answer, still streaming
    check("D. fresh chat syncs ?session= during streaming",
          "session=" in page.url, f"url={page.url}")
    # let it finish before the next check
    expect(page.locator("button", has_text="Send")).to_be_visible(timeout=60000)
    page.wait_for_timeout(500)

    # --- Fix A: fresh chat retains ?session= after first message ---
    check("A. fresh chat has ?session= in URL", "session=" in page.url, f"url={page.url}")

    # --- Fix B: refresh mid-(long)-stream reattaches live ---
    page.locator("textarea").first.fill(
        "Generate 5 QA pairs about astronomy, make a judge, and run an evaluation with claude on bedrock.")
    page.locator("button", has_text="Send").first.click()
    # Wait until the stream is genuinely in-flight and long-running. A tool
    # call (🔧) is the strongest signal, but accept any active Stop button too
    # (the agent may emit a text preamble first) — we just need it still
    # running when we refresh.
    streaming = False
    for _ in range(30):
        if "🔧" in page.inner_text("body"):
            streaming = True
            break
        page.wait_for_timeout(2000)
    if not streaming:
        # fall back: is it at least still streaming (Stop present)?
        streaming = page.locator("button", has_text="Stop").count() > 0
    check("B. long eval stream still running at refresh", streaming)

    status_calls.clear()
    reconnect_posts.clear()
    page.reload(wait_until="domcontentloaded")  # NOT networkidle: SSE stays open

    # reattach evidence: Stop button reappears quickly
    reattached = False
    for _ in range(20):
        if page.locator("button", has_text="Stop").count() > 0:
            reattached = True
            break
        page.wait_for_timeout(500)
    check("B. status probe fired on mount", len(status_calls) > 0)
    check("B. reconnect POST fired", len(reconnect_posts) > 0)
    check("B. reattached to live stream (Stop visible)", reattached)
    check("B. conversation restored after refresh",
          "astronomy" in page.inner_text("body").lower())

    # --- Fix C: can cancel the reattached stream cleanly (no orphan) ---
    if page.locator("button", has_text="Stop").count():
        page.locator("button", has_text="Stop").first.click()
        try:
            expect(page.locator("button", has_text="Send")).to_be_visible(timeout=25000)
            check("C. reattached stream cancels cleanly", True)
        except Exception:
            check("C. reattached stream cancels cleanly", False, "Send never returned")
    else:
        check("C. reattached stream cancels cleanly", False, "no Stop to cancel")

    check("no uncaught JS exceptions", len(page_errors) == 0, f"{page_errors[:3]}")
    browser.close()

print()
if failures:
    print(f"FAILED ({len(failures)}): {failures}")
    sys.exit(1)
print("ALL RECONNECT CHECKS PASSED")
