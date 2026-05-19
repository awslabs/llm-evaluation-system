"""Tests for the shared TOML-merge helper used by Codex.

The whole reason we use ``tomlkit`` instead of stdlib ``tomllib`` is to
preserve user comments and key order through the round-trip — these
tests pin that behavior.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import tomlkit

from eval_mcp.installers._toml_merge import merge_mcp_server


SERVER = {"command": "uvx", "args": ["--from", "llm-evaluation-system", "eval-mcp"]}


def test_creates_file_when_missing(tmp_path: Path):
    target = tmp_path / "config.toml"
    status, backup = merge_mcp_server(target, "eval", SERVER)
    assert status == "installed"
    assert backup is None
    doc = tomlkit.parse(target.read_text())
    assert doc["mcp_servers"]["eval"]["command"] == "uvx"
    assert list(doc["mcp_servers"]["eval"]["args"]) == SERVER["args"]


def test_preserves_other_mcp_servers(tmp_path: Path):
    target = tmp_path / "config.toml"
    target.write_text(
        '[mcp_servers.atlassian]\n'
        'command = "npx"\n'
        'args = ["-y", "@modelcontextprotocol/server-atlassian"]\n'
    )
    status, backup = merge_mcp_server(target, "eval", SERVER)
    assert status == "installed"
    assert backup is not None
    doc = tomlkit.parse(target.read_text())
    assert "atlassian" in doc["mcp_servers"]
    assert "eval" in doc["mcp_servers"]


def test_preserves_user_comments(tmp_path: Path):
    """The whole point of tomlkit over tomllib — round-trip without
    eating the user's comments."""
    target = tmp_path / "config.toml"
    target.write_text(
        '# my codex config\n'
        '\n'
        '[mcp_servers.atlassian]\n'
        '# pinned for the platform team\n'
        'command = "npx"\n'
        'args = ["-y", "atlassian-mcp"]\n'
    )
    merge_mcp_server(target, "eval", SERVER)
    text = target.read_text()
    assert "# my codex config" in text
    assert "# pinned for the platform team" in text


def test_preserves_top_level_settings(tmp_path: Path):
    target = tmp_path / "config.toml"
    target.write_text(
        'model = "claude-opus-4-7"\n'
        'theme = "dark"\n'
    )
    merge_mcp_server(target, "eval", SERVER)
    doc = tomlkit.parse(target.read_text())
    assert doc["model"] == "claude-opus-4-7"
    assert doc["theme"] == "dark"
    assert "eval" in doc["mcp_servers"]


def test_skips_when_already_present(tmp_path: Path):
    target = tmp_path / "config.toml"
    target.write_text('[mcp_servers.eval]\ncommand = "old"\n')
    original = target.read_text()
    status, backup = merge_mcp_server(target, "eval", SERVER, force=False)
    assert status == "skipped"
    assert backup is None
    assert target.read_text() == original


def test_force_overwrites_existing(tmp_path: Path):
    target = tmp_path / "config.toml"
    target.write_text('[mcp_servers.eval]\ncommand = "old"\n')
    status, backup = merge_mcp_server(target, "eval", SERVER, force=True)
    assert status == "replaced"
    assert backup is not None
    doc = tomlkit.parse(target.read_text())
    assert doc["mcp_servers"]["eval"]["command"] == "uvx"


def test_malformed_toml_raises(tmp_path: Path):
    target = tmp_path / "config.toml"
    target.write_text("this is = = not toml\n[[\n")
    with pytest.raises(ValueError, match="not valid TOML"):
        merge_mcp_server(target, "eval", SERVER)
