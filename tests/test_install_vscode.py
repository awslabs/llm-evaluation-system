"""Tests for the VS Code installer."""
from __future__ import annotations

import json

import pytest

from eval_mcp.installers.vscode import VSCodeInstaller


class _MockCompleted:
    def __init__(self, returncode=0, stderr=""):
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = ""


@pytest.fixture
def fake_subprocess(monkeypatch):
    recorded = []

    def fake_run(cmd, **kwargs):
        recorded.append(cmd)
        return _MockCompleted()

    monkeypatch.setattr(
        "eval_mcp.installers.vscode.subprocess.run", fake_run
    )
    monkeypatch.setattr(
        "eval_mcp.installers.vscode.shutil.which",
        lambda cmd: "/usr/bin/code" if cmd == "code" else None,
    )
    return recorded


def test_skips_without_force(fake_subprocess):
    """Without --force, the VS Code installer can't tell whether the
    user already has an eval entry, so it refuses to clobber."""
    result = VSCodeInstaller().install(force=False)
    assert result.status == "skipped"
    assert "force" in result.message.lower()
    assert fake_subprocess == []


def test_install_with_force_passes_correct_payload(fake_subprocess):
    result = VSCodeInstaller().install(force=True)
    assert result.status == "installed"
    assert len(fake_subprocess) == 1
    cmd = fake_subprocess[0]
    assert cmd[0] == "code"
    assert cmd[1] == "--add-mcp"
    payload = json.loads(cmd[2])
    assert payload == {
        "name": "eval",
        "command": "uvx",
        "args": ["--from", "llm-evaluation-system", "eval-mcp"],
    }


def test_returns_failed_when_code_missing(monkeypatch):
    monkeypatch.setattr(
        "eval_mcp.installers.vscode.shutil.which", lambda cmd: None
    )
    result = VSCodeInstaller().install(force=True)
    assert result.status == "failed"
    assert "code" in result.message.lower()


def test_detect_uses_code_on_path(monkeypatch):
    monkeypatch.setattr(
        "eval_mcp.installers.vscode.has_command",
        lambda cmd: cmd == "code",
    )
    assert VSCodeInstaller().detect() is True
    monkeypatch.setattr(
        "eval_mcp.installers.vscode.has_command", lambda cmd: False
    )
    assert VSCodeInstaller().detect() is False
