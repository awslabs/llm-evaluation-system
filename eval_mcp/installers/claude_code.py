"""Claude Code installer — shells out to ``claude mcp add``.

We use the official CLI rather than editing ``~/.claude.json`` directly
so we stay forward-compatible with Claude Code's internal config layout.
"""
from __future__ import annotations

import shutil
import subprocess

from .base import MCP_ARGS, MCP_COMMAND, SERVER_NAME, Result
from .detect import has_command, has_dir


class ClaudeCodeInstaller:
    name = "claude-code"
    display = "Claude Code"

    def detect(self) -> bool:
        return has_command("claude") or has_dir("~/.claude")

    def install(self, *, force: bool = False) -> Result:
        if not shutil.which("claude"):
            return Result(
                self.display,
                "failed",
                "`claude` not on PATH — install the Claude Code CLI first",
            )

        # `claude mcp list` is cheaper than `claude mcp get <name>` (the
        # latter spawns the server for a health check). Parse for "<name>:".
        listing = subprocess.run(
            ["claude", "mcp", "list"], capture_output=True, text=True
        )
        already = any(
            line.startswith(f"{SERVER_NAME}:")
            for line in listing.stdout.splitlines()
        )

        if already and not force:
            return Result(
                self.display,
                "skipped",
                f"'{SERVER_NAME}' already registered (use --force to overwrite)",
            )

        if already and force:
            subprocess.run(
                ["claude", "mcp", "remove", SERVER_NAME, "-s", "user"],
                capture_output=True, text=True,
            )

        result = subprocess.run(
            [
                "claude", "mcp", "add", SERVER_NAME, "-s", "user", "--",
                MCP_COMMAND, *MCP_ARGS,
            ],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return Result(
                self.display,
                "failed",
                f"`claude mcp add` exited {result.returncode}: {result.stderr.strip()}",
            )

        status = "replaced" if already else "installed"
        return Result(self.display, status, "user scope")

    def restart_hint(self) -> str:
        return "Restart Claude Code (reload window or quit + reopen)."
