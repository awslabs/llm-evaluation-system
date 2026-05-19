"""Tests for the Codex installer (TOML merge)."""
from __future__ import annotations

from pathlib import Path

import pytest
import tomlkit

from eval_mcp.installers import codex as codex_mod
from eval_mcp.installers.codex import CodexInstaller


@pytest.fixture
def redirect_config(tmp_path: Path, monkeypatch):
    target = tmp_path / "config.toml"
    monkeypatch.setattr(codex_mod, "CONFIG_PATH", target)
    return target


def test_install_writes_to_codex_path(redirect_config: Path):
    result = CodexInstaller().install()
    assert result.status == "installed"
    doc = tomlkit.parse(redirect_config.read_text())
    assert doc["mcp_servers"]["eval"]["command"] == "uvx"
    assert list(doc["mcp_servers"]["eval"]["args"]) == [
        "--from", "llm-evaluation-system", "eval-mcp"
    ]


def test_install_preserves_other_servers_and_comments(redirect_config: Path):
    redirect_config.write_text(
        '# user-managed codex config\n'
        '[mcp_servers.atlassian]\n'
        'command = "npx"\n'
        'args = ["-y", "atlassian-mcp"]\n'
    )
    result = CodexInstaller().install()
    assert result.status == "installed"
    text = redirect_config.read_text()
    assert "# user-managed codex config" in text
    doc = tomlkit.parse(text)
    assert "atlassian" in doc["mcp_servers"]
    assert "eval" in doc["mcp_servers"]


def test_install_skips_when_already_present(redirect_config: Path):
    redirect_config.write_text('[mcp_servers.eval]\ncommand = "old"\n')
    result = CodexInstaller().install()
    assert result.status == "skipped"


def test_install_force_overwrites(redirect_config: Path):
    redirect_config.write_text('[mcp_servers.eval]\ncommand = "old"\n')
    result = CodexInstaller().install(force=True)
    assert result.status == "replaced"
    doc = tomlkit.parse(redirect_config.read_text())
    assert doc["mcp_servers"]["eval"]["command"] == "uvx"


def test_install_returns_failed_on_malformed_toml(redirect_config: Path):
    redirect_config.write_text("nope = = =\n[[\n")
    result = CodexInstaller().install()
    assert result.status == "failed"
    assert "TOML" in result.message


def test_detect_uses_codex_dir(monkeypatch):
    monkeypatch.setattr(
        "eval_mcp.installers.codex.has_dir", lambda p: p == "~/.codex"
    )
    assert CodexInstaller().detect() is True
    monkeypatch.setattr(
        "eval_mcp.installers.codex.has_dir", lambda p: False
    )
    assert CodexInstaller().detect() is False
