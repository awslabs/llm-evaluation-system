"""VS Code installer — shells out to ``code --add-mcp``."""
from __future__ import annotations

import json
import shutil
import subprocess

from .base import MCP_ARGS, MCP_COMMAND, SERVER_NAME, Result
from .detect import has_command


class VSCodeInstaller:
    name = "vscode"
    display = "VS Code"

    def detect(self) -> bool:
        return has_command("code")

    def install(self, *, force: bool = False) -> Result:
        # `code --add-mcp` is idempotent in recent VS Code releases — it
        # overwrites the existing entry. We surface that as "replaced"
        # when --force is set, otherwise refuse without --force so a
        # re-run doesn't silently clobber user customizations.
        if not shutil.which("code"):
            return Result(
                self.display,
                "failed",
                "`code` not on PATH — install VS Code CLI ('code') first",
            )

        payload = json.dumps({
            "name": SERVER_NAME,
            "command": MCP_COMMAND,
            "args": MCP_ARGS,
        })
        # `code --add-mcp` doesn't expose a "check if present" subcommand,
        # so without --force we can't safely no-op. Skip with a message.
        if not force:
            return Result(
                self.display,
                "skipped",
                "VS Code's CLI can't check for an existing entry — pass --force to install",
            )

        result = subprocess.run(
            ["code", "--add-mcp", payload],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return Result(
                self.display,
                "failed",
                f"`code --add-mcp` exited {result.returncode}: {result.stderr.strip()}",
            )
        return Result(self.display, "installed", "user settings")

    def restart_hint(self) -> str:
        return "Reload the VS Code window (Cmd/Ctrl-Shift-P → 'Reload Window')."
