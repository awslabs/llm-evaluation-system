"""Tests for the Cursor installer (same shape as Kiro — JSON merge)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval_mcp.installers import cursor as cursor_mod
from eval_mcp.installers.cursor import CursorInstaller


@pytest.fixture
def redirect_config(tmp_path: Path, monkeypatch):
    target = tmp_path / "mcp.json"
    monkeypatch.setattr(cursor_mod, "CONFIG_PATH", target)
    return target


def test_install_writes_to_cursor_path(redirect_config: Path):
    result = CursorInstaller().install()
    assert result.status == "installed"
    data = json.loads(redirect_config.read_text())
    assert data["mcpServers"]["eval"]["command"] == "uvx"


def test_install_preserves_other_servers(redirect_config: Path):
    redirect_config.write_text(json.dumps({
        "mcpServers": {"foo": {"command": "bar"}}
    }))
    result = CursorInstaller().install()
    assert result.status == "installed"
    data = json.loads(redirect_config.read_text())
    assert set(data["mcpServers"].keys()) == {"foo", "eval"}


def test_install_skips_when_already_present(redirect_config: Path):
    redirect_config.write_text(json.dumps({
        "mcpServers": {"eval": {"command": "old"}}
    }))
    result = CursorInstaller().install()
    assert result.status == "skipped"


def test_install_force_overwrites(redirect_config: Path):
    redirect_config.write_text(json.dumps({
        "mcpServers": {"eval": {"command": "old"}}
    }))
    result = CursorInstaller().install(force=True)
    assert result.status == "replaced"
    assert json.loads(redirect_config.read_text())["mcpServers"]["eval"]["command"] == "uvx"


def test_detect_uses_cursor_dir(monkeypatch):
    monkeypatch.setattr(
        "eval_mcp.installers.cursor.has_dir", lambda p: p == "~/.cursor"
    )
    assert CursorInstaller().detect() is True
    monkeypatch.setattr(
        "eval_mcp.installers.cursor.has_dir", lambda p: False
    )
    assert CursorInstaller().detect() is False
