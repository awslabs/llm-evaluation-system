"""Tests for the Kiro installer.

Thin wrapper around ``_json_merge`` — these tests verify it points at
the right config path and surfaces the right Result statuses. The
merge behavior itself is covered in detail by
``test_install_json_merge.py``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval_mcp.installers import kiro as kiro_mod
from eval_mcp.installers.kiro import KiroInstaller


@pytest.fixture
def redirect_config(tmp_path: Path, monkeypatch):
    target = tmp_path / "settings" / "mcp.json"
    monkeypatch.setattr(kiro_mod, "CONFIG_PATH", target)
    return target


def test_install_writes_expected_shape(redirect_config: Path):
    result = KiroInstaller().install()
    assert result.status == "installed"
    data = json.loads(redirect_config.read_text())
    assert data == {
        "mcpServers": {
            "eval": {
                "command": "uvx",
                "args": ["--from", "llm-evaluation-system", "eval-mcp"],
            }
        }
    }


def test_install_preserves_other_servers(redirect_config: Path):
    redirect_config.parent.mkdir(parents=True, exist_ok=True)
    redirect_config.write_text(json.dumps({
        "mcpServers": {"sentry": {"url": "https://mcp.sentry.dev/mcp"}}
    }))
    result = KiroInstaller().install()
    assert result.status == "installed"
    assert result.backup_path is not None and Path(result.backup_path).exists()
    data = json.loads(redirect_config.read_text())
    assert "sentry" in data["mcpServers"]
    assert "eval" in data["mcpServers"]


def test_install_skips_when_already_registered(redirect_config: Path):
    redirect_config.parent.mkdir(parents=True, exist_ok=True)
    redirect_config.write_text(json.dumps({
        "mcpServers": {"eval": {"command": "old"}}
    }))
    result = KiroInstaller().install()
    assert result.status == "skipped"
    # original unchanged
    assert json.loads(redirect_config.read_text())["mcpServers"]["eval"] == {"command": "old"}


def test_install_force_overwrites(redirect_config: Path):
    redirect_config.parent.mkdir(parents=True, exist_ok=True)
    redirect_config.write_text(json.dumps({
        "mcpServers": {"eval": {"command": "old"}}
    }))
    result = KiroInstaller().install(force=True)
    assert result.status == "replaced"
    assert json.loads(redirect_config.read_text())["mcpServers"]["eval"]["command"] == "uvx"


def test_install_returns_failed_on_malformed_json(redirect_config: Path):
    redirect_config.parent.mkdir(parents=True, exist_ok=True)
    redirect_config.write_text("{not json")
    result = KiroInstaller().install()
    assert result.status == "failed"
    assert "JSON" in result.message


def test_detect_uses_kiro_dir(monkeypatch):
    monkeypatch.setattr(
        "eval_mcp.installers.kiro.has_dir", lambda p: p == "~/.kiro"
    )
    assert KiroInstaller().detect() is True
    monkeypatch.setattr(
        "eval_mcp.installers.kiro.has_dir", lambda p: False
    )
    assert KiroInstaller().detect() is False
