"""Resource-sharing E2E — datasets, judges, optimizations, documents + cascade.

Companion to verify_grant_isolation.py (which covers evals). Proves the SAME
grant model extends to the other resource types: a viewer is denied an
ungranted resource, allowed once granted, and that sharing an optimization
CASCADES grants to its referenced dataset/judge/eval logs.

Two identities: the nginx stub pins :4001 to local-user, so we hit the backend
container directly with an explicit X-Forwarded-User. OWNER plants data + grants
via the API; VIEWER reads. Needs the local stack up (make dev). Exit 0 = passed.
"""

import json
import subprocess
import sys

COMPOSE = ["docker", "compose", "-f", "local/compose.yaml"]
OWNER = "local-user"       # stub identity; we plant data under them
VIEWER = "rs-viewer"       # second identity via header

failures = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def _exec(cmd):
    r = subprocess.run(COMPOSE + ["exec", "-T", "backend", "sh", "-c", cmd],
                       capture_output=True, text=True)
    return r.stdout


def _curl(path, user, method="GET", body=None):
    parts = ["curl", "-s", "-o", "/tmp/rb", "-w", "%{http_code}",
             "-H", f"'X-Forwarded-User: {user}'", "-X", method]
    if body is not None:
        parts += ["-H", "'Content-Type: application/json'", "--data", f"'{body}'"]
    parts.append(f"'http://localhost:8080{path}'")
    code = _exec(" ".join(parts)).strip()
    rb = _exec("cat /tmp/rb 2>/dev/null || true")
    try:
        status = int(code[-3:])
    except ValueError:
        status = -1
    return status, rb


def _psql(sql):
    # psql lives in the postgres container, NOT the backend container.
    r = subprocess.run(
        COMPOSE + ["exec", "-T", "postgres", "psql", "-U", "node", "-d", "evaldb",
                   "-tA", "-c", sql],
        capture_output=True, text=True,
    )
    return r.stdout


def plant():
    """Plant a dataset, judge, and an optimization (referencing both) under the
    OWNER's store, plus a real eval log the optimization points at."""
    import base64
    store = f"/data/users/{OWNER}/store"
    logs = f"/data/users/{OWNER}/logs"
    _exec(f"mkdir -p {store}/datasets {store}/judges {store}/optimizations {logs}")

    ds = {"type": "dataset", "id": "ds-rs1", "name": "rs-dataset",
          "tests": [{"vars": {"question": "q", "golden_answer": "a"}}],
          "source": {"kind": "manual"}, "created_at": 1, "num_samples": 1}
    jd = {"type": "judge", "id": "jud-rs1", "name": "rs-judge",
          "config": {"domain": "general", "criteria": [{"name": "Accuracy"}]}, "created_at": 1}
    # Reuse a real eval log so the cascade's eval_run_id points at something real.
    src = _exec(f"ls {logs}/*.eval 2>/dev/null | head -1").strip()
    run_id = ""
    if src:
        _exec(f"cp '{src}' {logs}/rs-opt-eval.eval")
        run_id = _exec(
            "python3 -c \"import asyncio;"
            "from inspect_ai.log import read_eval_log_async;"
            "from inspect_ai._view.common import list_eval_logs_async;"
            "asyncio.run((lambda: None)())\" 2>/dev/null"
        ).strip()
    opt = {"type": "optimization", "id": "opt-rs1", "dataset": "rs-dataset",
           "judge": "rs-judge", "providers": ["m1"], "winner_iter": 1,
           "winner_test_score": 0.9, "iterations_run": 1, "status": "complete",
           "created_at": 1, "history": [{"iter": 0, "eval_run_id": None}],
           "initial_prompt": "p0", "winner_prompt": "p1", "test_results": []}

    for sub, obj in (("datasets", ds), ("judges", jd), ("optimizations", opt)):
        b64 = base64.b64encode(json.dumps(obj).encode()).decode()
        _exec(f"echo {b64} | base64 -d > {store}/{sub}/{obj['id']}.json")


def cleanup():
    _exec(f"rm -rf /data/users/{VIEWER}")
    _exec(f"rm -f /data/users/{OWNER}/store/datasets/ds-rs1.json "
          f"/data/users/{OWNER}/store/judges/jud-rs1.json "
          f"/data/users/{OWNER}/store/optimizations/opt-rs1.json "
          f"/data/users/{OWNER}/logs/rs-opt-eval.eval")
    _psql(f"DELETE FROM eval_grants WHERE owner_id='{OWNER}';")
    _psql(f"DELETE FROM users WHERE id='{VIEWER}';")


def share(resource_type_base, resource_id):
    body = json.dumps({"principal_type": "user", "principal_id": VIEWER,
                       "resource_id": resource_id, "role": "viewer"})
    return _curl(f"/api/{resource_type_base}/shares", OWNER, method="POST", body=body)


def run():
    # ---- DATASET ----
    dpath = f"/api/datasets/ds-rs1?owner={OWNER}"
    status, _ = _curl(dpath, VIEWER)
    check("dataset denied pre-grant (403)", status == 403, f"status={status}")
    status, body = share("datasets", "ds-rs1")
    check("owner shares dataset (200)", status == 200, f"status={status} {body}")
    status, _ = _curl(dpath, VIEWER)
    check("dataset allowed post-grant (200)", status == 200, f"status={status}")
    # viewer's dataset list now includes it, tagged shared
    status, body = _curl("/api/datasets", VIEWER)
    check("viewer dataset list includes shared", status == 200 and "rs-dataset" in body)

    # ---- JUDGE ----
    jpath = f"/api/judges/jud-rs1?owner={OWNER}"
    status, _ = _curl(jpath, VIEWER)
    check("judge denied pre-grant (403)", status == 403, f"status={status}")
    status, _ = share("judges", "jud-rs1")
    status, _ = _curl(jpath, VIEWER)
    check("judge allowed post-grant (200)", status == 200, f"status={status}")

    # ---- OPTIMIZATION + CASCADE ----
    # Revoke the standalone dataset/judge grants first so we can prove the
    # optimization share re-grants them via cascade.
    _psql(f"DELETE FROM eval_grants WHERE owner_id='{OWNER}';")
    opath = f"/api/optimizations/detail?id=opt-rs1&owner={OWNER}"
    status, _ = _curl(opath, VIEWER)
    check("optimization denied pre-grant (403)", status == 403, f"status={status}")
    status, body = share("optimizations", "opt-rs1")
    check("owner shares optimization (200)", status == 200, f"status={status} {body}")
    cascaded = (json.loads(body).get("cascaded") if status == 200 else []) or []
    check("cascade granted dataset+judge",
          any("dataset" in c for c in cascaded) and any("judge" in c for c in cascaded),
          f"cascaded={cascaded}")
    status, _ = _curl(opath, VIEWER)
    check("optimization allowed post-grant (200)", status == 200, f"status={status}")
    # cascade should have re-opened the dataset too
    status, _ = _curl(f"/api/datasets/ds-rs1?owner={OWNER}", VIEWER)
    check("cascade re-opened dataset (200)", status == 200, f"status={status}")

    # ---- cross-resource isolation: a dataset grant must NOT open the judge ----
    _psql(f"DELETE FROM eval_grants WHERE owner_id='{OWNER}';")
    share("datasets", "ds-rs1")
    status, _ = _curl(f"/api/judges/jud-rs1?owner={OWNER}", VIEWER)
    check("dataset grant does NOT open judge (403)", status == 403, f"status={status}")

    # ---- revoke ----
    # Start clean so exactly one dataset grant exists, then revoke it and prove
    # access is gone.
    _psql(f"DELETE FROM eval_grants WHERE owner_id='{OWNER}';")
    share("datasets", "ds-rs1")
    status, _ = _curl(f"/api/datasets/ds-rs1?owner={OWNER}", VIEWER)
    check("dataset readable before revoke (200)", status == 200, f"status={status}")
    status, body = _curl("/api/datasets/shares", OWNER)
    shares = json.loads(body).get("shares", []) if status == 200 else []
    for s in shares:
        _curl(f"/api/datasets/shares/{s['id']}", OWNER, method="DELETE")
    status, _ = _curl(f"/api/datasets/ds-rs1?owner={OWNER}", VIEWER)
    check("dataset denied after revoke (403)", status == 403, f"status={status}")


if __name__ == "__main__":
    try:
        plant()
        run()
    finally:
        cleanup()
    print()
    if failures:
        print(f"RESOURCE-SHARING FAILED ({len(failures)}): {failures}")
        sys.exit(1)
    print("ALL RESOURCE-SHARING CHECKS PASSED")
