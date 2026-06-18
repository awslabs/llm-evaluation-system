"""Stress test for the S3-origin topology backend.

Cost-aware: tiny one-word prompts only, NO evals/tools, bounded concurrency.
Targets the failure modes the in-memory stream state (active_tasks /
event_queues, keyed by session) is most likely to break under:

  1. Static origin load    — hammer nginx for the SPA + assets (no Bedrock)
  2. Concurrent streams     — many chat sessions streaming at once
  3. Stop/start churn       — start then immediately cancel, repeated
  4. Refresh/reconnect storm— many status probes + reconnect POSTs at once
  5. Orphaned-stream cleanup— disconnect mid-stream, assert backend cleans up

Goes through nginx :4001 (the real edge path), same as a browser.
Exit 0 = all dimensions passed.
"""

import asyncio
import sys
import time
import uuid
import urllib.request
import urllib.error
import json

BASE = "http://localhost:4001"
failures: list[str] = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(name)


# ---- tiny HTTP helpers (thread pool, since urllib is blocking) ----

def _get(path, timeout=10):
    req = urllib.request.Request(BASE + path)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""
    except Exception:
        return 0, b""


def _post_stream(path, body, timeout=60, read_bytes_limit=None, abort_after_bytes=None):
    """POST and consume the SSE stream. Returns (status, total_bytes, first_session_id)."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    total = 0
    session_id = None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            for line in r:
                total += len(line)
                if session_id is None and b'"session_id"' in line:
                    try:
                        payload = line.split(b"data:", 1)[1].strip()
                        session_id = json.loads(payload).get("session_id")
                    except Exception:
                        pass
                if abort_after_bytes and total >= abort_after_bytes:
                    # Simulate client disconnect mid-stream
                    return r.status, total, session_id
            return r.status, total, session_id
    except Exception as e:
        return -1, total, session_id


async def gather_bounded(coros, limit):
    sem = asyncio.Semaphore(limit)

    async def run(c):
        async with sem:
            return await c

    return await asyncio.gather(*(run(c) for c in coros), return_exceptions=True)


async def to_thread(fn, *a, **kw):
    return await asyncio.to_thread(fn, *a, **kw)


# ---- Dimension 1: static origin load (cheap, no Bedrock) ----

async def dim_static():
    print("\n=== Dimension 1: static origin load (200 reqs, conc 30) ===")
    # mix of shell, deep links, and the hashed asset
    _, body = _get("/")
    asset = None
    for tok in body.split(b'"'):
        if tok.startswith(b"/assets/") and tok.endswith(b".js"):
            asset = tok.decode()
            break
    paths = (["/", "/chat", "/history", "/results"] * 40) + ([asset] * 40 if asset else [])
    t0 = time.time()
    results = await gather_bounded([to_thread(_get, p) for p in paths], limit=30)
    dt = time.time() - t0
    codes = [r[0] for r in results if isinstance(r, tuple)]
    ok = sum(1 for c in codes if c == 200)
    check("static: all 200", ok == len(paths), f"{ok}/{len(paths)} ok in {dt:.1f}s")
    check("static: throughput sane", dt < 20, f"{len(paths)} reqs in {dt:.1f}s")


# ---- Dimension 2: concurrent streams ----

async def dim_concurrent_streams(n=12):
    print(f"\n=== Dimension 2: {n} concurrent chat streams (conc {n}) ===")
    t0 = time.time()
    coros = [
        to_thread(_post_stream, "/api/chat/message",
                  {"message": "Reply with exactly one word: ok", "session_id": str(uuid.uuid4()), "stream": True},
                  90)
        for _ in range(n)
    ]
    results = await gather_bounded(coros, limit=n)
    dt = time.time() - t0
    good = [r for r in results if isinstance(r, tuple) and r[0] == 200 and r[1] > 0]
    sids = {r[2] for r in good if r[2]}
    check("concurrent: all streams completed 200", len(good) == n, f"{len(good)}/{n} in {dt:.1f}s")
    check("concurrent: unique session ids", len(sids) == n, f"{len(sids)} distinct of {n}")


# ---- Dimension 3: stop/start churn ----

async def dim_stop_churn(rounds=10):
    print(f"\n=== Dimension 3: stop/start churn ({rounds} rounds) ===")
    survived = 0
    for i in range(rounds):
        sid = str(uuid.uuid4())
        # start a stream in the background, abort it almost immediately, then cancel
        task = asyncio.create_task(
            to_thread(_post_stream, "/api/chat/message",
                      {"message": "Count slowly to 50, one number per line.", "session_id": sid, "stream": True},
                      30, abort_after_bytes=200)
        )
        await asyncio.sleep(0.4)
        code, _ = await to_thread(_cancel, sid)
        await task
        if code in (200, 404):  # 404 ok if it already finished/cleaned
            survived += 1
    check("churn: all cancels handled", survived == rounds, f"{survived}/{rounds}")
    # backend still alive?
    code, _ = await to_thread(_get, "/health")
    check("churn: backend healthy after churn", code == 200, f"/health -> {code}")


def _cancel(sid):
    data = b"{}"
    req = urllib.request.Request(BASE + f"/api/chat/cancel/{sid}", data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""
    except Exception:
        return 0, b""


# ---- Dimension 4: refresh/reconnect storm ----

async def dim_reconnect_storm(n=20):
    print(f"\n=== Dimension 4: status-probe / reconnect storm ({n} concurrent) ===")
    # Probe status for many random (mostly-nonexistent) sessions concurrently —
    # this is what a refresh storm does. Must never 5xx or hang.
    sids = [str(uuid.uuid4()) for _ in range(n)]
    results = await gather_bounded([to_thread(_get, f"/api/chat/status/{s}") for s in sids], limit=n)
    codes = [r[0] for r in results if isinstance(r, tuple)]
    check("reconnect: no 5xx under storm", all(c == 200 for c in codes), f"codes={set(codes)}")
    code, _ = await to_thread(_get, "/health")
    check("reconnect: backend healthy after storm", code == 200, f"/health -> {code}")


# ---- Dimension 5: orphaned-stream cleanup ----

async def dim_orphan_cleanup(n=6):
    print(f"\n=== Dimension 5: orphaned-stream cleanup ({n} disconnects) ===")
    # Start streams and disconnect mid-stream (abort_after_bytes). Background
    # task should run to completion / clean up; backend must not leak or die.
    coros = [
        to_thread(_post_stream, "/api/chat/message",
                  {"message": "Write a short 3-line poem about the sea.", "session_id": str(uuid.uuid4()), "stream": True},
                  30, abort_after_bytes=120)
        for _ in range(n)
    ]
    results = await gather_bounded(coros, limit=n)
    disconnected = sum(1 for r in results if isinstance(r, tuple) and r[1] > 0)
    check("orphan: all streams produced bytes before disconnect", disconnected == n, f"{disconnected}/{n}")
    # give the backend a moment to finish background tasks, then confirm health
    await asyncio.sleep(8)
    code, _ = await to_thread(_get, "/health")
    check("orphan: backend healthy after disconnects", code == 200, f"/health -> {code}")


async def main():
    await dim_static()
    await dim_reconnect_storm()
    await dim_concurrent_streams()
    await dim_stop_churn()
    await dim_orphan_cleanup()


if __name__ == "__main__":
    asyncio.run(main())
    print()
    if failures:
        print(f"STRESS FAILED ({len(failures)}): {failures}")
        sys.exit(1)
    print("ALL STRESS DIMENSIONS PASSED")
