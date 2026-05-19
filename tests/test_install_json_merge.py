"""Tests for the shared JSON-merge helper used by Kiro and Cursor.

Concentrating the file-touching logic in one helper means we only have
to verify backup/atomic-write/key-collision behavior in one place.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval_mcp.installers._json_merge import merge_mcp_server


SERVER = {"command": "uvx", "args": ["--from", "llm-evaluation-system", "eval-mcp"]}


def test_creates_file_when_missing(tmp_path: Path):
    target = tmp_path / "mcp.json"
    status, backup = merge_mcp_server(target, "eval", SERVER)
    assert status == "installed"
    assert backup is None  # no original to back up
    data = json.loads(target.read_text())
    assert data == {"mcpServers": {"eval": SERVER}}


def test_creates_parent_dirs(tmp_path: Path):
    target = tmp_path / "deeply" / "nested" / "mcp.json"
    status, _ = merge_mcp_server(target, "eval", SERVER)
    assert status == "installed"
    assert target.exists()


def test_empty_file_becomes_object(tmp_path: Path):
    target = tmp_path / "mcp.json"
    target.write_text("")
    status, backup = merge_mcp_server(target, "eval", SERVER)
    assert status == "installed"
    assert backup is not None and Path(backup).exists()
    assert json.loads(target.read_text()) == {"mcpServers": {"eval": SERVER}}


def test_preserves_other_mcp_servers(tmp_path: Path):
    target = tmp_path / "mcp.json"
    target.write_text(json.dumps({
        "mcpServers": {
            "sentry": {"url": "https://mcp.sentry.dev/mcp"},
            "github": {"command": "npx", "args": ["-y", "gh-mcp"]},
        }
    }))
    status, backup = merge_mcp_server(target, "eval", SERVER)
    assert status == "installed"
    assert backup is not None
    data = json.loads(target.read_text())
    assert set(data["mcpServers"].keys()) == {"sentry", "github", "eval"}
    assert data["mcpServers"]["sentry"] == {"url": "https://mcp.sentry.dev/mcp"}
    assert data["mcpServers"]["eval"] == SERVER


def test_preserves_unrelated_top_level_keys(tmp_path: Path):
    """Some IDEs (Kiro) put other settings alongside `mcpServers`. Don't
    nuke them."""
    target = tmp_path / "mcp.json"
    target.write_text(json.dumps({
        "theme": "dark",
        "telemetry": False,
        "mcpServers": {},
    }))
    merge_mcp_server(target, "eval", SERVER)
    data = json.loads(target.read_text())
    assert data["theme"] == "dark"
    assert data["telemetry"] is False
    assert "eval" in data["mcpServers"]


def test_skips_when_already_present(tmp_path: Path):
    target = tmp_path / "mcp.json"
    original = {"mcpServers": {"eval": {"command": "old"}}}
    target.write_text(json.dumps(original))
    status, backup = merge_mcp_server(target, "eval", SERVER, force=False)
    assert status == "skipped"
    assert backup is None
    # file unchanged
    assert json.loads(target.read_text()) == original


def test_force_overwrites_existing(tmp_path: Path):
    target = tmp_path / "mcp.json"
    target.write_text(json.dumps({"mcpServers": {"eval": {"command": "old"}}}))
    status, backup = merge_mcp_server(target, "eval", SERVER, force=True)
    assert status == "replaced"
    assert backup is not None
    assert json.loads(target.read_text())["mcpServers"]["eval"] == SERVER


def test_malformed_json_raises(tmp_path: Path):
    target = tmp_path / "mcp.json"
    target.write_text("{not valid json")
    with pytest.raises(ValueError, match="not valid JSON"):
        merge_mcp_server(target, "eval", SERVER)


def test_top_level_array_raises(tmp_path: Path):
    target = tmp_path / "mcp.json"
    target.write_text(json.dumps([1, 2, 3]))
    with pytest.raises(ValueError, match="not a JSON object"):
        merge_mcp_server(target, "eval", SERVER)


def test_mcp_servers_not_object_raises(tmp_path: Path):
    target = tmp_path / "mcp.json"
    target.write_text(json.dumps({"mcpServers": ["bad"]}))
    with pytest.raises(ValueError, match="is not an object"):
        merge_mcp_server(target, "eval", SERVER)


def test_custom_mcp_servers_key(tmp_path: Path):
    """Some IDEs might use a different key name. The helper should let us
    pass it in."""
    target = tmp_path / "mcp.json"
    status, _ = merge_mcp_server(
        target, "eval", SERVER, mcp_servers_key="servers"
    )
    assert status == "installed"
    data = json.loads(target.read_text())
    assert "servers" in data and "eval" in data["servers"]
