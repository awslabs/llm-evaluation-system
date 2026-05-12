# For coding agents: installing eval-mcp

Follow this when a user asks you to install eval-mcp into their IDE.

## 1. Prerequisite check

User needs `uv`. Check: `command -v uv`. If missing, tell them to run `curl -LsSf https://astral.sh/uv/install.sh | sh` (or `brew install uv` on macOS).

## 2. Pick the IDE

Detect from what's present in the user's home:

| IDE | Check |
|---|---|
| Claude Code | `claude` on PATH or `~/.claude/` exists |
| Cursor | `~/.cursor/` exists |
| VS Code | `code` on PATH |
| Kiro | `~/.kiro/` exists |
| Codex CLI | `~/.codex/` exists |

If multiple, ask the user which.

## 3. Install

Run the one-command install for that IDE:

- **Claude Code**: `claude mcp add eval -s user -- uvx --from llm-evaluation-system eval-mcp`
- **Cursor**: open `cursor://anysphere.cursor-deeplink/mcp/install?name=eval&config=eyJjb21tYW5kIjoidXZ4IiwiYXJncyI6WyItLWZyb20iLCJsbG0tZXZhbHVhdGlvbi1zeXN0ZW0iLCJldmFsLW1jcCJdfQ==` (or edit `~/.cursor/mcp.json`)
- **VS Code**: `code --add-mcp '{"name":"eval","command":"uvx","args":["--from","llm-evaluation-system","eval-mcp"]}'`
- **Kiro**: merge into `~/.kiro/settings/mcp.json` under `mcpServers.eval` — same JSON shape as Cursor. Back up first, atomic write.
- **Codex**: append to `~/.codex/config.toml`: `[mcp_servers.eval]\ncommand = "uvx"\nargs = ["--from", "llm-evaluation-system", "eval-mcp"]`

For any manual JSON/TOML edit: back up first, merge — never clobber, atomic write.

## 4. Warm the cache

Run this so the user's first IDE launch is instant instead of waiting ~60s for uvx to fetch the package:

```bash
uvx --from llm-evaluation-system eval-mcp --help >/dev/null
```

## 5. Ask about S3 team sharing

> Want to share eval results, datasets, judges, and reports with teammates via a shared S3 bucket?

If **yes, team already has a bucket**:
```bash
uvx --from llm-evaluation-system eval-mcp init <bucket-name>
```

If **yes, creating a new bucket** (first on the team):
```bash
git clone https://github.com/awslabs/llm-evaluation-system.git /tmp/eval-mcp-infra
cd /tmp/eval-mcp-infra/infra/modules/eval-logs-bucket
terraform init
terraform apply -var="bucket_name=<bucket-name>"
```
Then `uvx --from llm-evaluation-system eval-mcp init <bucket-name>`. Tell the user: teammates just run `eval-mcp init <bucket-name>` — no Terraform needed.

Suggested bucket name if they don't have a preference: `eval-mcp-$(aws sts get-caller-identity --query Account --output text)` — globally unique, easy to remember.

If **no**: skip. They can enable it later with `eval-mcp init <bucket>`.

## 6. Tell the user to restart

- Claude Code / Cursor / VS Code: reload/restart the window or app
- Kiro: save mcp.json — picks up automatically
- Codex: must restart the CLI

## Uninstall

Reverse step 3 (remove the `eval` entry), restart.
