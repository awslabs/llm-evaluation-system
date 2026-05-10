# Installing eval-mcp in an IDE

This document is written for a coding agent (Claude Code, Cursor, etc.) to follow
when a user asks to install `eval-mcp`. It describes where each supported IDE
stores its MCP configuration, the JSON snippet to add, and how to do it safely.

A human can follow it too — just read each "Instructions for the agent" block as
steps.

---

## The MCP snippet

Every IDE uses the same underlying snippet. It differs only in where in the JSON
tree it lives (see per-IDE sections below).

```json
{
  "command": "uvx",
  "args": ["--from", "llm-evaluation-system", "eval-mcp"],
  "timeout": 120000
}
```

`timeout: 120000` (2 minutes) covers the first-run case where uvx downloads
the package and its dependencies. Subsequent runs are instant.

---

## Prerequisites

The user must have `uv` installed. Check with:

```bash
command -v uv
```

If missing, tell the user to run:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

(or `brew install uv` on macOS.)

---

## Install steps (all IDEs)

For each IDE the user has installed:

1. **Locate the config file** (see per-IDE sections).
2. **Back it up**: `cp <path> <path>.bak-<timestamp>`. Always.
3. **Parse the file** as JSON (some IDEs allow comments — strip them if needed
   or use a JSONC-aware parser).
4. **Merge** the snippet under the correct key (see per-IDE sections). Preserve
   every other key as-is.
5. **Write atomically**: write to `<path>.tmp`, then rename to `<path>`.
6. **Warm the uvx cache** so the IDE's first MCP spawn is instant:

   ```bash
   uvx --from llm-evaluation-system eval-mcp --help >/dev/null
   ```

   Run this once during install. Takes 30-90s the first time the user has ever
   used this package; near-instant after.

7. **Tell the user** exactly which file you changed and that they need to
   restart the IDE to activate the MCP.

---

## Per-IDE details

### Claude Code

**User config path (pick the first that exists, else create the first):**
- `~/.claude/settings.json`
- `~/.config/claude/settings.json`

**Workspace config path** (use instead of user config only if the user explicitly says
"install in this project"):
- `./.claude/settings.local.json`

**Where to merge:** top-level key `mcpServers.eval`:

```json
{
  "mcpServers": {
    "eval": {
      "command": "uvx",
      "args": ["--from", "llm-evaluation-system", "eval-mcp"],
      "timeout": 120000
    }
  }
}
```

**Restart:** fully quit and reopen Claude Code (the CLI) or run `/restart` if the
user is already in a Claude Code session. The MCP list is loaded at session
start.

---

### Cursor

**User config path:**
- `~/.cursor/mcp.json`

**Workspace config path** (only if the user asks for a project-scoped install):
- `./.cursor/mcp.json`

**Where to merge:** top-level key `mcpServers.eval` (same shape as Claude Code).

**Restart:** fully quit and reopen Cursor (Cmd+Q on macOS, Alt+F4 on Windows/Linux).
"Reload Window" usually works but a full quit is the safe recommendation.

---

### VS Code (with GitHub Copilot MCP support)

**User config path (platform-dependent):**
- macOS: `~/Library/Application Support/Code/User/settings.json`
- Linux: `~/.config/Code/User/settings.json`
- Windows: `%APPDATA%\Code\User\settings.json`

**Where to merge:** top-level key `github.copilot.mcp.servers.eval`. VS Code settings
is JSONC (allows comments and trailing commas) — use a JSONC-aware parser. If
you must use plain JSON, at minimum strip `//` and `/* */` comments before
parsing, and preserve the original file's formatting on write.

```jsonc
{
  // …existing settings untouched…
  "github.copilot.mcp.servers": {
    "eval": {
      "command": "uvx",
      "args": ["--from", "llm-evaluation-system", "eval-mcp"],
      "timeout": 120000
    }
  }
}
```

VS Code also supports a one-shot CLI install as a fallback:
```bash
code --add-mcp '{"name":"eval","command":"uvx","args":["--from","llm-evaluation-system","eval-mcp"],"timeout":120000}'
```

**Restart:** "Developer: Reload Window" from the command palette, or quit and
reopen VS Code.

---

### Kiro

**User config path:**
- `~/.kiro/settings/mcp.json`

**Workspace config path:**
- `./.kiro/settings/mcp.json`

**Where to merge:** top-level key `mcpServers.eval` (same shape as Claude Code).

**Restart:** quit and reopen Kiro.

---

## Detection heuristics

Before asking the user which IDE to install into, detect which are present:

| IDE | Detect if this path exists |
|---|---|
| Claude Code | `~/.claude/` directory or `claude` command on PATH |
| Cursor | `~/.cursor/` directory or Cursor app installed |
| VS Code | `~/Library/Application Support/Code/` (macOS), equivalents on Linux/Windows, or `code` on PATH |
| Kiro | `~/.kiro/` directory |

If none are detected, print the snippet and paths and let the user copy manually.

---

## Safety rules — non-negotiable

- **Always back up** before writing (`<path>.bak-<ISO-timestamp>`).
- **Never clobber** existing config — merge only.
- **Atomic write** (tmp + rename). Never write in-place.
- **Show a diff** before writing and confirm with the user.
- **Never write secrets** into the config. The MCP uses the user's AWS
  credentials from the environment; do not embed keys.

---

## Uninstall

Same flow in reverse: locate the IDE's config, remove the `eval` entry from
`mcpServers` (or `github.copilot.mcp.servers`), atomic write, tell the user to
restart.

---

## Versioning of this guidance

When the snippet or IDE config shape changes, bump this document. The
`eval-mcp install` CLI command always prints the version bundled with the
installed `llm-evaluation-system` package, so what the agent reads always
matches what the user has installed.
