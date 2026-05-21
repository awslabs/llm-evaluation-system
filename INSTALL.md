# For coding agents: installing eval-mcp

Follow this when a user asks you to install eval-mcp into their IDE.

## Happy path (one command)

```bash
uvx --from llm-evaluation-system eval-mcp install --yes
```

The subcommand detects which IDEs are present (Claude Code, Kiro, VS Code,
Cursor, Codex), registers `eval` in each, and warms the uvx cache so the
first IDE launch isn't 60s of "disconnected". Then tell the user to
restart whichever IDE(s) it touched.

If the user wants a single IDE only:
```bash
uvx --from llm-evaluation-system eval-mcp install --ide claude-code --yes
```

Valid `--ide` values: `claude-code`, `kiro`, `vscode`, `cursor`, `codex`.
Comma-separate for multiple.

If `eval` is already registered, the subcommand skips with a message.
Pass `--force` to overwrite.

## Prerequisite

User needs `uv`. Check: `command -v uv`. If missing, tell them to run
`curl -LsSf https://astral.sh/uv/install.sh | sh` (or `brew install uv`
on macOS).

## After install: S3 team sharing prompt

> Want to share eval results, datasets, judges, and reports with teammates via a shared S3 bucket?

If **yes, team already has a bucket**:
```bash
uvx --from llm-evaluation-system eval-mcp init <bucket-name>
```

If **yes, creating a new bucket** (first on the team). Prereq: AWS credentials configured — verify with `aws sts get-caller-identity` before running terraform.
```bash
git clone https://github.com/awslabs/llm-evaluation-system.git /tmp/eval-mcp-infra
cd /tmp/eval-mcp-infra/infra/eval-logs-bucket
terraform init
terraform apply -var="bucket_name=<logical-name>" -var="region=us-west-2"
```
The Terraform module appends the caller's AWS account ID, so the actual bucket created is `<logical-name>-<account-id>` — globally unique, no name collisions with other teams using the same example. The full name is printed under the `bucket_name` Terraform output.

Then `uvx --from llm-evaluation-system eval-mcp init <logical-name>` — `init` probes both the literal name and the account-suffixed name, persists whichever exists along with its region. Tell the user: teammates run `eval-mcp init <logical-name>` from the same AWS account — no Terraform needed.

Suggested logical name if they don't have a preference: `<team>-evals` (e.g. `growth-evals`, `platform-evals`). The account-ID suffix makes it unique without the user having to think about uniqueness.

If **no**: skip. They can enable it later with `eval-mcp init <logical-name>`.

## Fallback: manual per-IDE install

If `eval-mcp install` won't run for some reason (e.g. corporate network blocks PyPI
mid-install and the cache warm-up fails), here are the same operations done by hand:

- **Claude Code**: `claude mcp add eval -s user -- uvx --from llm-evaluation-system eval-mcp`
- **VS Code**: `code --add-mcp '{"name":"eval","command":"uvx","args":["--from","llm-evaluation-system","eval-mcp"]}'`
- **Kiro**: merge into `~/.kiro/settings/mcp.json` under `mcpServers.eval`:
  ```json
  {"mcpServers": {"eval": {"command": "uvx", "args": ["--from", "llm-evaluation-system", "eval-mcp"]}}}
  ```
- **Cursor**: same JSON shape, in `~/.cursor/mcp.json`. Or use the one-click deeplink: `cursor://anysphere.cursor-deeplink/mcp/install?name=eval&config=eyJjb21tYW5kIjoidXZ4IiwiYXJncyI6WyItLWZyb20iLCJsbG0tZXZhbHVhdGlvbi1zeXN0ZW0iLCJldmFsLW1jcCJdfQ==`
- **Codex**: append to `~/.codex/config.toml`: `[mcp_servers.eval]\ncommand = "uvx"\nargs = ["--from", "llm-evaluation-system", "eval-mcp"]`

For any manual JSON/TOML edit: back up first, merge — never clobber, atomic write.

Do **not** use `llm-evaluation-system@latest` in the IDE config — it forces a PyPI
resolution on every MCP start (~20s cold start) and causes the MCP to show
"disconnected" on first connect after any new release. Plain `llm-evaluation-system`
is what's documented across the MCP ecosystem (reference servers, `mcp-atlassian`,
etc.). Upgrade separately via `uv cache clean llm-evaluation-system`.

## Tell the user to restart

The `install` subcommand prints per-IDE restart hints. Quick recap:

- Claude Code / Cursor / VS Code: reload/restart the window or app
- Kiro: picks up `mcp.json` automatically — no restart needed
- Codex: must restart the CLI

## Uninstall

Per IDE: remove the `eval` entry from the relevant config (Claude Code:
`claude mcp remove eval -s user`; others: edit the JSON/TOML). Then restart.
