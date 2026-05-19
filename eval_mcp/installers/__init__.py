"""Per-IDE installers for ``eval-mcp install``.

Registry order is the order they appear in interactive prompts and the
end-of-run summary. Claude Code first because it's the primary target;
JSON-merge IDEs grouped together so the user can see what file work is
about to happen.
"""
from __future__ import annotations

from .base import Installer, Result
from .claude_code import ClaudeCodeInstaller
from .codex import CodexInstaller
from .cursor import CursorInstaller
from .kiro import KiroInstaller
from .vscode import VSCodeInstaller

REGISTRY: dict[str, Installer] = {
    inst.name: inst
    for inst in (
        ClaudeCodeInstaller(),
        KiroInstaller(),
        VSCodeInstaller(),
        CursorInstaller(),
        CodexInstaller(),
    )
}


__all__ = ["REGISTRY", "Installer", "Result"]
