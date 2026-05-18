"""HTTP-level tests for the Data Library endpoints in backend/api/main.py.

Storage is isolated under a tmp path via USER_STORAGE_BASE, and DATA_BUCKET
is forced empty so the local-filesystem JSON store is used.
"""
import os
from pathlib import Path
from typing import Generator

import pytest
from fastapi.testclient import TestClient


USER = "test-user"
AUTH = {"X-Forwarded-User": USER}


@pytest.fixture
def client(tmp_path: Path, monkeypatch) -> Generator[TestClient, None, None]:
    """Spin up a TestClient against a tmp storage root with one user."""
    monkeypatch.setenv("USER_STORAGE_BASE", str(tmp_path))
    monkeypatch.setenv("DATA_BUCKET", "")
    # Reload the module so storage helpers pick up the new env.
    import importlib
    import eval_mcp.core.user_storage as us
    import backend.api.main as main_mod
    importlib.reload(us)
    importlib.reload(main_mod)

    yield TestClient(main_mod.app)


# ---------- /api/datasets ----------


def _save_sample(name: str, n: int = 3, source=None):
    import eval_mcp.core.user_storage as us
    # Embed name into the test content so each dataset hashes to a unique id —
    # dataset_id is sha256(tests), so identical tests would collide otherwise.
    tests = [
        {"vars": {"question": f"{name}-q{i}", "golden_answer": f"{name}-a{i}"}}
        for i in range(n)
    ]
    return us.save_dataset_to_db(USER, name, tests, source=source)


def test_list_datasets_requires_auth(client):
    r = client.get("/api/datasets")
    assert r.status_code == 401


def test_list_datasets_empty(client):
    r = client.get("/api/datasets", headers=AUTH)
    assert r.status_code == 200
    assert r.json() == {"datasets": []}


def test_list_datasets_strips_tests_and_returns_source(client):
    _save_sample("imported_set", n=2, source={"kind": "imported", "origin": "x.csv"})
    _save_sample("synthetic_set", n=5, source={"kind": "synthetic", "mode": "document"})

    r = client.get("/api/datasets", headers=AUTH)
    assert r.status_code == 200
    items = r.json()["datasets"]
    assert len(items) == 2
    by_name = {d["name"]: d for d in items}

    assert by_name["imported_set"]["num_samples"] == 2
    assert by_name["imported_set"]["source"]["kind"] == "imported"
    assert by_name["imported_set"]["source"]["origin"] == "x.csv"
    assert "tests" not in by_name["imported_set"]  # list view must NOT include tests

    assert by_name["synthetic_set"]["source"]["kind"] == "synthetic"
    assert by_name["synthetic_set"]["source"]["mode"] == "document"


def test_list_datasets_search_filter(client):
    _save_sample("alpha", n=1)
    _save_sample("beta", n=1)

    r = client.get("/api/datasets?search=alp", headers=AUTH)
    assert r.status_code == 200
    names = [d["name"] for d in r.json()["datasets"]]
    assert names == ["alpha"]


# ---------- /api/datasets/{id} ----------


def test_get_dataset_detail_pagination(client):
    dsid = _save_sample("paged", n=125)

    r = client.get(f"/api/datasets/{dsid}?offset=0&limit=50", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 125
    assert body["offset"] == 0
    assert body["limit"] == 50
    assert len(body["tests"]) == 50
    assert body["tests"][0]["vars"]["question"] == "paged-q0"

    r = client.get(f"/api/datasets/{dsid}?offset=100&limit=50", headers=AUTH)
    body = r.json()
    assert len(body["tests"]) == 25  # 125 - 100
    assert body["tests"][0]["vars"]["question"] == "paged-q100"


def test_get_dataset_detail_not_found(client):
    r = client.get("/api/datasets/does-not-exist", headers=AUTH)
    assert r.status_code == 404


def test_get_dataset_detail_clamps_limit(client):
    dsid = _save_sample("clamp", n=10)
    # huge or negative limit should fall back to default
    r = client.get(f"/api/datasets/{dsid}?limit=9999", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["limit"] == 50


# ---------- PATCH ----------


def test_patch_dataset_rename(client):
    dsid = _save_sample("old_name", n=1)
    r = client.patch(
        f"/api/datasets/{dsid}",
        headers={**AUTH, "Content-Type": "application/json"},
        json={"name": "new_name"},
    )
    assert r.status_code == 200
    assert r.json()["name"] == "new_name"

    # Confirm via list view
    r = client.get("/api/datasets", headers=AUTH)
    names = [d["name"] for d in r.json()["datasets"]]
    assert "new_name" in names
    assert "old_name" not in names


def test_patch_dataset_replace_tests(client):
    dsid = _save_sample("editable", n=3)
    new_tests = [
        {"vars": {"question": "edited q1", "golden_answer": "edited a1"}},
        {"vars": {"question": "edited q2", "golden_answer": "edited a2"}},
    ]
    r = client.patch(
        f"/api/datasets/{dsid}",
        headers=AUTH,
        json={"tests": new_tests},
    )
    assert r.status_code == 200
    assert r.json()["total"] == 2

    detail = client.get(f"/api/datasets/{dsid}", headers=AUTH).json()
    assert detail["total"] == 2
    assert detail["tests"][0]["vars"]["question"] == "edited q1"


def test_patch_dataset_404(client):
    r = client.patch("/api/datasets/missing", headers=AUTH, json={"name": "x"})
    assert r.status_code == 404


# ---------- DELETE ----------


def test_delete_dataset(client):
    dsid = _save_sample("kill_me", n=1)
    r = client.delete(f"/api/datasets/{dsid}", headers=AUTH)
    assert r.status_code == 200
    assert r.json() == {"deleted": True}

    assert client.get(f"/api/datasets/{dsid}", headers=AUTH).status_code == 404


def test_delete_dataset_404(client):
    r = client.delete("/api/datasets/missing", headers=AUTH)
    assert r.status_code == 404


# ---------- CSV export ----------


def test_export_dataset_csv(client):
    import eval_mcp.core.user_storage as us
    tests = [
        {"vars": {"question": "Q with, comma", "golden_answer": 'A with "quotes"'}},
        {"vars": {"question": "Q2", "golden_answer": "A2", "category": "edge"}},
    ]
    dsid = us.save_dataset_to_db(USER, "csv_test", tests, source={"kind": "imported"})

    r = client.get(f"/api/datasets/{dsid}/export", headers=AUTH)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    body = r.text

    # CSV header must put question first then golden_answer (reading order),
    # then any extra vars discovered across rows.
    lines = body.strip().splitlines()
    header = lines[0].split(",")
    assert header[0] == "question"
    assert header[1] == "golden_answer"
    assert "category" in header  # extra var carried through

    # quoting via csv module: commas/quotes survive a round-trip
    import csv, io
    rows = list(csv.DictReader(io.StringIO(body)))
    assert rows[0]["question"] == "Q with, comma"
    assert rows[0]["golden_answer"] == 'A with "quotes"'
    assert rows[1]["category"] == "edge"


# ---------- /api/judges ----------


def _save_judge(name: str, criteria: list[str]):
    import eval_mcp.core.user_storage as us
    config = {
        "domain": "qa",
        "criteria": [{"name": c, "description": f"{c} desc"} for c in criteria],
    }
    return us.save_judge_to_db(USER, name, config)


def test_list_judges_empty(client):
    r = client.get("/api/judges", headers=AUTH)
    assert r.status_code == 200
    assert r.json() == {"judges": []}


def test_list_judges_and_detail(client):
    jid = _save_judge("strict_judge", ["accuracy", "completeness"])

    r = client.get("/api/judges", headers=AUTH)
    assert r.status_code == 200
    items = r.json()["judges"]
    assert len(items) == 1
    assert items[0]["name"] == "strict_judge"
    assert items[0]["domain"] == "qa"
    assert items[0]["criteria"] == ["accuracy", "completeness"]
    # list view must NOT include full config body
    assert "config" not in items[0]

    detail = client.get(f"/api/judges/{jid}", headers=AUTH).json()
    assert detail["id"] == jid
    assert detail["config"]["criteria"][0]["description"] == "accuracy desc"


def test_get_judge_404(client):
    r = client.get("/api/judges/missing", headers=AUTH)
    assert r.status_code == 404


def test_delete_judge(client):
    jid = _save_judge("disposable", ["c1"])
    r = client.delete(f"/api/judges/{jid}", headers=AUTH)
    assert r.status_code == 200
    assert client.get(f"/api/judges/{jid}", headers=AUTH).status_code == 404


# ---------- Provenance round-trip through save path ----------


def test_provenance_round_trip(client):
    """A dataset saved via save_dataset_to_db with a source descriptor must
    round-trip through both the list and detail endpoints unchanged.
    """
    import eval_mcp.core.user_storage as us
    src = {"kind": "synthetic", "mode": "agent", "agent": "my_agent.py"}
    dsid = us.save_dataset_to_db(USER, "prov_set", [{"vars": {"q": "x"}}], source=src)

    list_item = next(
        d for d in client.get("/api/datasets", headers=AUTH).json()["datasets"]
        if d["id"] == dsid
    )
    assert list_item["source"] == src

    detail = client.get(f"/api/datasets/{dsid}", headers=AUTH).json()
    assert detail["source"] == src


def test_delete_dataset_rejects_path_traversal(client):
    """Defense in depth: delete_dataset_from_db must reject ids that would
    escape the per-user store directory (e.g., URL-encoded ../../etc)."""
    import eval_mcp.core.user_storage as us
    # A traversal-shaped id should never make it to the filesystem.
    import pytest
    with pytest.raises(ValueError, match="invalid dataset_id"):
        us.delete_dataset_from_db(USER, "../../etc/passwd")


def test_delete_judge_rejects_path_traversal(client):
    import eval_mcp.core.user_storage as us
    import pytest
    with pytest.raises(ValueError, match="invalid judge_id"):
        us.delete_judge_from_db(USER, "../../etc/passwd")


def test_legacy_dataset_without_source_defaults_imported(client, tmp_path):
    """Records saved before provenance existed should still surface a source
    in API responses (defaulting to 'imported')."""
    import eval_mcp.core.user_storage as us
    import json as _json

    # Manually write a legacy-shaped JSON file (no `source` field).
    store_dir = us._get_json_store_dir(USER, "datasets")
    legacy_id = "legacy-0001"
    (store_dir / f"{legacy_id}.json").write_text(_json.dumps({
        "id": legacy_id,
        "name": "legacy",
        "type": "dataset",
        "tests": [{"vars": {"question": "q", "golden_answer": "a"}}],
        "created_at": 1700000000000,
    }))

    r = client.get(f"/api/datasets/{legacy_id}", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["source"] == {"kind": "imported"}
