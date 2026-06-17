"""Tenant-isolation E2E — the Inspect-viewer cross-tenant breach + compare/sample guard.

Battle-tests, against the live local stack (nginx :4001), that one logged-in
tenant (`local-user`) cannot read, list, download, or delete another tenant's
eval logs, and that /api/compare/sample's path guard can't be bypassed with a
crafted substring path.

How two tenants are simulated locally: the local auth stub pins every request
to `local-user`, so we can't "log in as bob". Instead we plant a victim
tenant's files directly inside the backend container (proving the breach from
the attacker's single identity) and assert local-user is denied. The planted
dir is removed in a finally.

Pre-fix this script FAILS (the breach reproduces). Post-fix it PASSES.
Needs the local stack up (`make dev`). Exit 0 = passed.
"""

import subprocess
import sys
import urllib.parse
import urllib.request
import urllib.error

BASE = "http://localhost:4001"
COMPOSE = ["docker", "compose", "-f", "local/compose.yaml"]
VICTIM = "victim-bob"
VICTIM_DIR = f"/data/users/{VICTIM}/logs"
VICTIM_FILE = f"{VICTIM_DIR}/secret.eval"
SECRET = "SECRET: bob private eval data"

failures: list[str] = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def _get(path):
    """GET BASE+path. Returns (status, body_text)."""
    req = urllib.request.Request(BASE + path)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return -1, str(e)


def _enc(p):
    return urllib.parse.quote(p, safe="")


def plant_victim():
    subprocess.run(
        COMPOSE + ["exec", "-T", "backend", "sh", "-c",
                   f"mkdir -p {VICTIM_DIR} && printf '%s' '{SECRET}' > {VICTIM_FILE}"],
        check=True, capture_output=True,
    )


def cleanup_victim():
    subprocess.run(
        COMPOSE + ["exec", "-T", "backend", "sh", "-c", f"rm -rf /data/users/{VICTIM}"],
        check=False, capture_output=True,
    )


def run():
    file_uri = f"file://{VICTIM_FILE}"

    # --- F1: cross-tenant DOWNLOAD must be denied (was 200 + secret bytes) ---
    status, body = _get(f"/api/log-download/{_enc(file_uri)}")
    check("F1. cross-tenant log-download denied", status == 403,
          f"status={status}")
    check("F1. victim secret NOT exfiltrated", SECRET not in body,
          "secret bytes returned in body")

    # --- F1: cross-tenant raw read path (/api/logs/<path>) denied ---
    status, _ = _get(f"/api/logs/{_enc(file_uri)}")
    check("F1. cross-tenant log read denied", status in (403, 404),
          f"status={status}")

    # --- F2: directory ENUMERATION of victim dir must be denied/empty ---
    status, body = _get(f"/api/logs?log_dir={_enc(VICTIM_DIR)}")
    leaked = "secret.eval" in body
    check("F2. cross-tenant dir enumeration blocked", status == 403 or not leaked,
          f"status={status} leaked_names={leaked}")

    # --- F2: arbitrary dir listing (/tmp) blocked ---
    status, body = _get(f"/api/logs?log_dir={_enc('/tmp')}")
    check("F2. arbitrary dir listing blocked", status == 403,
          f"status={status}")

    # --- F1: cross-tenant DELETE denied ---
    status, _ = _get(f"/api/log-delete/{_enc(file_uri)}")
    check("F1. cross-tenant log-delete denied", status == 403, f"status={status}")
    # confirm the file still exists after the delete attempt
    r = subprocess.run(COMPOSE + ["exec", "-T", "backend", "sh", "-c",
                                  f"test -f {VICTIM_FILE} && echo EXISTS || echo GONE"],
                       capture_output=True, text=True)
    check("F1. victim file survived delete attempt", "EXISTS" in r.stdout, r.stdout.strip())

    # --- F3: compare/sample crafted-substring bypass denied (was 500 = guard passed) ---
    crafted = "/data/users/victim-bob/logs/users/local-user/x.eval"
    status, _ = _get(f"/api/compare/sample?log_file={_enc(crafted)}&sample_id=1")
    check("F3. compare/sample substring bypass denied", status == 403,
          f"status={status} (500 means guard passed)")

    # --- F3: traversal-style path aimed at victim denied ---
    traversal = "/data/users/local-user/logs/../../victim-bob/logs/secret.eval"
    status, _ = _get(f"/api/compare/sample?log_file={_enc(traversal)}&sample_id=1")
    check("F3. compare/sample traversal denied", status == 403,
          f"status={status}")

    # --- F2: default root listing must be SCOPED to own logs, not leak victim ---
    # /api/logs with no param defaults to the shared root; the mapping policy
    # must rewrite it to the caller's own dir so it can't enumerate victim-bob.
    status, body = _get("/api/logs")
    check("F2. default root listing scoped (no victim leak)",
          status == 200 and "victim-bob" not in body,
          f"status={status} victim_in_body={'victim-bob' in body}")

    # --- regression: own results flow still works ---
    status, _ = _get("/api/compare/groups")
    check("regression: own /api/compare/groups works", status == 200, f"status={status}")
    status, _ = _get("/api/log-dir")
    check("regression: own /api/log-dir works", status == 200, f"status={status}")
    status, _ = _get("/api/log-files")
    check("regression: own /api/log-files works", status == 200, f"status={status}")


if __name__ == "__main__":
    try:
        plant_victim()
        run()
    finally:
        cleanup_victim()

    print()
    if failures:
        print(f"TENANT-ISOLATION FAILED ({len(failures)}): {failures}")
        sys.exit(1)
    print("ALL TENANT-ISOLATION CHECKS PASSED")
