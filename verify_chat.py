"""Live chat + stop-button verification — full web app, single origin :4001.

Runs against the dockerized backend (real Bedrock) with the static Vite SPA
served by nginx. Exercises the full-mode surface the viewer can't:

  - the chat page loads in mode:"full" (Chat/History nav visible)
  - sending a message starts an SSE stream (assistant message appears)
  - the Stop button appears mid-stream and halts it ([Request cancelled])
  - after the post-cancel cooldown, the input re-enables and resend works

This is the same-pod cancel path. Cross-pod cancel is EKS-only and not
covered here. Needs live Bedrock — a stream long enough to click Stop.

Exit 0 = all passed.
"""

import sys
from playwright.sync_api import sync_playwright, expect

BASE = "http://localhost:4001"
failures: list[str] = []
page_errors: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.on("pageerror", lambda e: page_errors.append(str(e)))

    # 1. Full-mode chat page loads and shows the full nav.
    page.goto(BASE + "/chat", wait_until="networkidle")
    page.wait_for_timeout(1500)  # let AuthContext resolve mode:"full"
    nav = page.locator("nav").first
    # Nav labels are upper-cased via CSS; compare case-insensitively.
    nav_text = (nav.inner_text() if nav.count() else "").lower()
    check("chat page loads", page.locator("textarea").count() >= 1)
    check("full mode shows Chat + History nav",
          "chat" in nav_text and "history" in nav_text, f"nav={nav_text!r}")

    # 2. Send a message → assistant streaming message appears.
    ta = page.locator("textarea").first
    # A long, token-heavy prompt so the stream is reliably still in-flight when
    # we click Stop (a short answer can complete before the click lands, which
    # legitimately yields no cancel marker).
    ta.fill("Write a detailed 600-word essay about the history of cartography, "
            "with several paragraphs. Take your time and be thorough.")
    page.locator("button", has_text="Send").first.click()

    # The Stop button only renders while isStreaming. Wait for it.
    stop_btn = page.locator("button", has_text="Stop")
    try:
        expect(stop_btn).to_be_visible(timeout=15000)
        check("stream started (Stop button visible)", True)
    except Exception:
        check("stream started (Stop button visible)", False,
              "Stop never appeared — stream didn't start (Bedrock?)")

    # 3. Let a little text stream in, then click Stop.
    page.wait_for_timeout(1500)
    if stop_btn.count():
        stop_btn.first.click()
        # Button flips to "Stopping…" immediately (isCancelling).
        try:
            expect(page.locator("button", has_text="Stopping")).to_be_visible(timeout=3000)
            check("Stop shows 'Stopping…' state", True)
        except Exception:
            check("Stop shows 'Stopping…' state", False, "no Stopping… state")

        # Stream halts: the streaming button goes away and Send returns.
        try:
            expect(page.locator("button", has_text="Send")).to_be_visible(timeout=15000)
            check("stream halted (Send button returned)", True)
        except Exception:
            check("stream halted (Send button returned)", False, "still streaming after Stop")

        # The [Request cancelled] marker is appended when the backend's
        # `cancelled` SSE frame arrives — a beat after the button flips back.
        # Poll for it rather than asserting on the first frame.
        cancelled = False
        for _ in range(10):
            if "cancelled" in page.inner_text("body").lower():
                cancelled = True
                break
            page.wait_for_timeout(500)
        check("cancellation reflected in transcript", cancelled,
              "no [Request cancelled] marker after 5s")
    else:
        check("Stop shows 'Stopping…' state", False, "no Stop button to click")
        check("stream halted (Send button returned)", False, "no Stop button to click")
        check("cancellation reflected in transcript", False, "no Stop button to click")

    # 4. After cooldown, resend works (input re-enables, new stream starts).
    page.wait_for_timeout(2500)  # post-cancel cooldown is ~2s
    ta2 = page.locator("textarea").first
    check("input re-enabled after cooldown", ta2.is_enabled())
    if ta2.is_enabled():
        ta2.fill("Just say OK.")
        page.locator("button", has_text="Send").first.click()
        try:
            expect(page.locator("button").filter(
                has_text="Stop")).to_be_visible(timeout=15000)
            check("resend after stop starts a new stream", True)
        except Exception:
            check("resend after stop starts a new stream", False, "resend didn't stream")

    check("no uncaught JS exceptions", len(page_errors) == 0,
          f"{len(page_errors)}: {page_errors[:3]}")

    browser.close()

print()
if failures:
    print(f"FAILED ({len(failures)}): {failures}")
    sys.exit(1)
print("ALL CHAT CHECKS PASSED")
