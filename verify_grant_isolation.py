"""Grant-isolation E2E — cross-user eval SHARING authorization.

Companion to verify_tenant_isolation.py. Where that script proves a tenant
CANNOT reach another's evals at all, this proves the sharing feature opens
exactly the right door and no more: a viewer is denied an ungranted eval,
allowed once a grant exists, and denied again after revoke — all scoped to the
specific (owner, group) the grant names.

Two identities locally: the nginx stub pins every request through :4001 to
`local-user`, so we drive the backend container directly with an explicit
X-Forwarded-User header (the stub only APPENDS its header, and Starlette's
.get returns the first, so our value wins). `local-user` plays the OWNER
(matching the data we plant under their dir); `viewer-carol` plays the VIEWER.

Needs the local stack up (`make dev`). Exit 0 = passed.
"""

import json
import subprocess
import sys

COMPOSE = ["docker", "compose", "-f", "local/compose.yaml"]

OWNER = "local-user"          # the stub identity; we plant data under them
VIEWER = "viewer-carol"       # a second identity, sent via header
GROUP = "run-abc123"          # a fake Inspect run_id / group id
OTHER_GROUP = "run-zzz999"    # owner has this too, but never shares it
SECRET = "SECRET-jury-reasoning-for-abc123"

failures: list[str] = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def _exec(cmd: str) -> str:
    """Run a shell command inside the backend container, return stdout."""
    r = subprocess.run(
        COMPOSE + ["exec", "-T", "backend", "sh", "-c", cmd],
        capture_output=True, text=True,
    )
    return r.stdout


def _curl(path: str, user: str, method: str = "GET", body: str | None = None):
    """Hit the backend directly (bypassing the nginx stub) as `user`.

    Returns (status:int, body:str). Uses curl inside the container so we can
    set an arbitrary X-Forwarded-User — the way two identities are simulated.
    """
    parts = [
        "curl", "-s", "-o", "/tmp/resp.body", "-w", "%{http_code}",
        "-H", f"'X-Forwarded-User: {user}'",
        "-X", method,
    ]
    if body is not None:
        parts += ["-H", "'Content-Type: application/json'", "--data", f"'{body}'"]
    parts.append(f"'http://localhost:8080{path}'")
    code = _exec(" ".join(parts)).strip()
    resp_body = _exec("cat /tmp/resp.body 2>/dev/null || true")
    try:
        status = int(code[-3:])
    except ValueError:
        status = -1
    return status, resp_body


def plant_owner_data():
    """Plant the owner's precomputed groups/detail JSON + a raw .eval log.

    Mirrors the on-disk layout user_storage.py writes:
      /data/users/<owner>/store/eval_results/_groups.json
      /data/users/<owner>/store/eval_results/detail_<group>.json
      /data/users/<owner>/logs/<group>.eval
    """
    store = f"/data/users/{OWNER}/store/eval_results"
    logs = f"/data/users/{OWNER}/logs"
    groups = json.dumps({"groups": [
        {"id": GROUP, "task": "t", "configName": "shared-cfg", "created": "2026-06-01T00:00:00",
         "models": ["m1"], "sampleCount": 3, "status": "success", "scores": {}},
        {"id": OTHER_GROUP, "task": "t", "configName": "private-cfg", "created": "2026-06-02T00:00:00",
         "models": ["m1"], "sampleCount": 3, "status": "success", "scores": {}},
    ]})
    detail = json.dumps({"groupId": GROUP, "task": "t", "models": ["m1"],
                         "criteria": [], "criteriaDescriptions": {}, "aggregate": {},
                         "samples": [], "stats": {}, "note": SECRET})
    _exec(f"mkdir -p {store} {logs}")
    # Use base64 to avoid shell-quoting hell with the JSON payloads.
    import base64
    g64 = base64.b64encode(groups.encode()).decode()
    d64 = base64.b64encode(detail.encode()).decode()
    _exec(f"echo {g64} | base64 -d > {store}/_groups.json")
    _exec(f"echo {d64} | base64 -d > {store}/detail_{GROUP}.json")
    _exec(f"printf '%s' '{SECRET}' > {logs}/{GROUP}.eval")


def cleanup():
    _exec(f"rm -rf /data/users/{VIEWER}")
    _exec(f"rm -f /data/users/{OWNER}/store/eval_results/_groups.json "
          f"/data/users/{OWNER}/store/eval_results/detail_{GROUP}.json "
          f"/data/users/{OWNER}/logs/{GROUP}.eval")
    # Best-effort: drop grants, team rows, and the test users this run created.
    sql = (
        f"DELETE FROM eval_grants WHERE owner_id='{OWNER}'; "
        f"DELETE FROM team_members WHERE team_id IN (SELECT id FROM teams WHERE created_by='{OWNER}'); "
        f"DELETE FROM teams WHERE created_by='{OWNER}'; "
        f"DELETE FROM users WHERE id LIKE 'deploytest-%';"
    )
    _exec(f"psql -h postgres -U node -d evaldb -c \"{sql}\" 2>/dev/null || true")


def _grant_count() -> str:
    return _exec(
        "psql -h postgres -U node -d evaldb -tA -c "
        f"\"SELECT count(*) FROM eval_grants WHERE owner_id='{OWNER}';\" 2>/dev/null"
    ).strip()


def run():
    detail_path = f"/api/compare/detail?group_id={GROUP}&owner={OWNER}"
    sample_log = f"/data/users/{OWNER}/logs/{GROUP}.eval"
    sample_path = (
        f"/api/compare/sample?log_file={sample_log}&sample_id=1"
        f"&group_id={GROUP}&owner={OWNER}"
    )

    # --- Owner can always read their own eval (sanity) ---
    status, _ = _curl(f"/api/compare/detail?group_id={GROUP}", OWNER)
    check("owner reads own detail", status == 200, f"status={status}")

    # --- BEFORE grant: viewer denied the shared eval ---
    status, body = _curl(detail_path, VIEWER)
    check("viewer denied ungranted detail (403)", status == 403, f"status={status}")
    check("secret not leaked pre-grant", SECRET not in body)

    status, _ = _curl(sample_path, VIEWER)
    check("viewer denied ungranted sample (403)", status == 403, f"status={status}")

    # Viewer's own /groups must NOT include the owner's eval yet.
    status, body = _curl("/api/compare/groups", VIEWER)
    check("viewer groups excludes ungranted eval",
          status == 200 and "shared-cfg" not in body, f"status={status}")

    # --- Owner grants THIS group (run-abc123) to the viewer ---
    grant_body = json.dumps({"principal_type": "user", "principal_id": VIEWER,
                             "group_id": GROUP, "role": "viewer"})
    status, body = _curl("/api/compare/shares", OWNER, method="POST", body=grant_body)
    check("owner creates grant (200)", status == 200, f"status={status} body={body}")

    # --- AFTER grant: viewer allowed the shared eval ---
    status, body = _curl(detail_path, VIEWER)
    check("viewer reads granted detail (200)", status == 200, f"status={status}")
    check("granted detail returns secret", SECRET in body)

    status, _ = _curl(sample_path, VIEWER)
    # Sample read may 404 (no matching sample id in our minimal log) but must
    # NOT be 403 — authorization passed. 403 would mean the grant didn't apply.
    check("viewer sample read authorized (not 403)", status != 403, f"status={status}")

    # Viewer's /groups now includes the shared eval, tagged shared.
    status, body = _curl("/api/compare/groups", VIEWER)
    check("viewer groups includes granted eval",
          status == 200 and "shared-cfg" in body, f"status={status}")

    # --- Scope: the grant on run-abc123 must NOT leak the un-shared group ---
    status, body = _curl(
        f"/api/compare/detail?group_id={OTHER_GROUP}&owner={OWNER}", VIEWER)
    check("grant does NOT leak un-shared group (403)", status == 403,
          f"status={status}")
    check("viewer groups excludes un-shared group", "private-cfg" not in body)

    # --- Revoke: viewer denied again ---
    status, body = _curl("/api/compare/shares", OWNER)
    shares = json.loads(body) if status == 200 else {"shares": []}
    grant_id = shares["shares"][0]["id"] if shares["shares"] else ""
    status, _ = _curl(f"/api/compare/shares/{grant_id}", OWNER, method="DELETE")
    check("owner revokes grant (200)", status == 200, f"status={status}")

    status, body = _curl(detail_path, VIEWER)
    check("viewer denied after revoke (403)", status == 403, f"status={status}")
    check("secret not leaked post-revoke", SECRET not in body)

    # --- TEAM grant: create team as owner, add a 3rd user, share to team ---
    TEAMMATE = "deploytest-dave"
    ct = json.dumps({"name": "deploy-team"})
    status, body = _curl("/api/teams", OWNER, method="POST", body=ct)
    check("owner creates team (200)", status == 200, f"status={status} body={body}")
    team_id = (json.loads(body).get("id") if status == 200 else "") or ""
    # add teammate as member
    am = json.dumps({"user_id": TEAMMATE, "role": "member"})
    status, _ = _curl(f"/api/teams/{team_id}/members", OWNER, method="POST", body=am)
    check("owner adds teammate (200)", status == 200, f"status={status}")
    # teammate denied before team grant
    status, _ = _curl(detail_path, TEAMMATE)
    check("teammate denied before team grant (403)", status == 403, f"status={status}")
    # share group to the team
    tg = json.dumps({"principal_type": "team", "principal_id": team_id,
                     "group_id": GROUP, "role": "viewer"})
    status, _ = _curl("/api/compare/shares", OWNER, method="POST", body=tg)
    check("owner shares to team (200)", status == 200, f"status={status}")
    # teammate (member) now allowed
    status, body = _curl(detail_path, TEAMMATE)
    check("team member reads granted detail (200)", status == 200, f"status={status}")
    # a non-member is still denied
    status, _ = _curl(detail_path, "deploytest-stranger")
    check("non-member denied team-shared eval (403)", status == 403, f"status={status}")

    # --- ORG grant: share to everyone ---
    og = json.dumps({"principal_type": "org", "principal_id": None,
                     "group_id": GROUP, "role": "viewer"})
    status, _ = _curl("/api/compare/shares", OWNER, method="POST", body=og)
    check("owner shares to org (200)", status == 200, f"status={status}")
    status, _ = _curl(detail_path, "deploytest-anyone")
    check("any user reads org-shared eval (200)", status == 200, f"status={status}")

    # --- editor/owner roles rejected (read-only v1) ---
    bad = json.dumps({"principal_type": "user", "principal_id": VIEWER,
                      "group_id": GROUP, "role": "editor"})
    status, _ = _curl("/api/compare/shares", OWNER, method="POST", body=bad)
    check("editor role rejected (400)", status == 400, f"status={status}")


if __name__ == "__main__":
    try:
        plant_owner_data()
        run()
    finally:
        cleanup()

    print()
    if failures:
        print(f"GRANT-ISOLATION FAILED ({len(failures)}): {failures}")
        sys.exit(1)
    print("ALL GRANT-ISOLATION CHECKS PASSED")
