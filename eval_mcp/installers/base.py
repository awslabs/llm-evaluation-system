"""Per-IDE installer base + shared constants.

Every installer answers two questions: "is this IDE on the machine?"
and "register eval-mcp into it." The dispatcher (``cli.py install``)
iterates the registry, asks each one ``detect()``, then routes to
``install()`` based on user flags or interactive choice.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

# MCP server name as registered in every IDE. Hardcoded — changing it
# would orphan installs that used the old name.
SERVER_NAME = "eval"

# The command every IDE invokes to start the MCP server. Plain
# `llm-evaluation-system` (no @latest) — `@latest` forces a PyPI
# resolution on every MCP start (~20s cold start) and causes the MCP
# to show "disconnected" on first connect after any new release.
MCP_COMMAND = "uvx"
MCP_ARGS = ["--from", "llm-evaluation-system", "eval-mcp"]


@dataclass
class Result:
    """Outcome of one installer's ``install()`` call.

    The dispatcher prints these as a summary at the end so the user
    sees what actually happened for each IDE.
    """

    ide: str                              # human-readable, e.g. "Claude Code"
    status: str                           # installed | replaced | skipped | failed | not-detected
    message: str = ""                     # extra context for the summary line
    backup_path: str | None = None        # set by JSON/TOML installers when they touched a file


class Installer(Protocol):
    name: str        # CLI flag form: "claude-code", "kiro", ...
    display: str     # human form: "Claude Code", "Kiro", ...

    def detect(self) -> bool: ...
    def install(self, *, force: bool = False) -> Result: ...
    def restart_hint(self) -> str: ...
