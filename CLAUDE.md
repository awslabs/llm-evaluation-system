# CLAUDE.md

This is the canonical agent-facing doc for the repo — Claude Code reads it directly, and [`AGENTS.md`](./AGENTS.md) is a thin pointer back here so other tools that follow the [agents.md](https://agents.md) convention (Codex, Cursor) land in the same place.

## What's in this repo

Two deployables that share some code:

- **`eval_mcp/`** — the MCP package published to PyPI as `llm-evaluation-system` (entry point `eval-mcp`). Self-contained, no database, no web app. This is what 99% of users install.
- **`backend/` + `frontend/`** — the optional EKS web app (FastAPI chat + Next.js UI + Cognito auth). `./deploy.sh` is its entry point; `make dev` runs it locally via Docker Compose.

The same `frontend/` source builds two artifacts: the Next.js app for the web deployment (`next build`), and a static export bundled into `eval_mcp/viewer_static/` for the MCP's local results viewer (`npm run build:viewer`). Changing frontend code therefore affects the PyPI wheel — the viewer static is package data per `pyproject.toml`.

## Key files

| File | Purpose |
|------|---------|
| `eval_mcp/server.py` | Unified MCP server — every tool is registered here |
| `eval_mcp/tools/` | Tool handlers (QA gen, judge, config, run, …) |
| `eval_mcp/core/bedrock_client.py` | Bedrock client + cross-region inference + API-key auth |
| `eval_mcp/core/judge_config.py` | Default judge models and criteria |
| `eval_mcp/provider_pricing.json` | Source of truth for model pricing — required when adding a model |
| `backend/core/agent.py` | EKS web app's agent system prompt + loop (the MCP itself doesn't host an agent) |
| `Makefile` | Local dev commands (`make dev`, `make logs`, `make restart`, `make stop`, `make release`) |

Full system architecture + diagrams: [ARCHITECTURE.md](./ARCHITECTURE.md).

## Commands

### MCP development (the common path)

```bash
uv venv && uv pip install -e .             # editable install
.venv/bin/eval-mcp                         # run as stdio MCP (what IDEs invoke)
.venv/bin/eval-mcp view                    # results viewer on :4001
.venv/bin/eval-mcp serve                   # HTTP MCP on :8002 (self-host path)
```

Point Claude Code at your editable install by setting `command` in `~/.claude.json`'s `eval` entry to `/abs/path/.venv/bin/eval-mcp`. Then `/mcp` → reconnect `eval` after each edit — no reinstall. Details in [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md#3-point-claude-code-at-your-local-build-permanent-dev-setup).

### Tests

```bash
.venv/bin/pytest tests/                                # full suite
.venv/bin/pytest tests/test_run_eval.py                # one file
.venv/bin/pytest tests/test_run_eval.py::test_name     # one test
.venv/bin/pytest -k "qa_allocation"                    # by keyword
```

Pytest is **only useful for narrow deterministic logic** (parsing, validation, regex). End-to-end coverage requires running the MCP from Claude Code — mocks of Bedrock/subprocesses/user-dirs produce false greens. See `docs/DEVELOPMENT.md` section 2.

### Frontend / viewer

```bash
cd frontend
npm install                  # first time only
npm run build:viewer         # rebuild + copy to eval_mcp/viewer_static/
npm run dev                  # standalone Next dev server on :3000
npm run lint                 # next lint
```

`build:viewer` runs `BUILD_MODE=export next build` and replaces `eval_mcp/viewer_static/`. Run it whenever you change frontend source if you want the local MCP viewer to reflect it.

### Local full-stack (web app)

```bash
AWS_PROFILE=my-profile make dev          # docker compose, all services, hot reload
make logs s=backend                       # tail one service
make restart s=backend                    # restart one with fresh creds
make stop                                 # docker compose down
make clean                                # also wipe volumes
```

Open http://127.0.0.1:4001. See [local/README.md](local/README.md).

### Release

`make release` (patch) / `make release-minor` / `make release-major` from a clean `main`. Tags `vX.Y.Z`, pushes, GitHub Actions builds the wheel (frontend rebuilt in CI) and publishes to PyPI via trusted publishing. Version is derived from the tag by `setuptools-scm` — **never** add a static `version` to `pyproject.toml`. Full ship workflow in the [ship-it skill](./.claude/skills/ship-it/SKILL.md).

## Architecture

### MCP server flow

User chats with an IDE → IDE invokes MCP tools registered in `eval_mcp/server.py` → handlers in `eval_mcp/tools/*.py` call into:
- `eval_mcp/core/bedrock_client.py` — Bedrock + cross-region inference + API-key auth.
- `eval_mcp/subprocess_runner.py` + `eval_mcp/_agent_launcher.py` — Inspect AI runs spawn as isolated subprocesses (NOT in-process), so a cancelled eval can't take down the MCP.
- `eval_mcp/otlp_receiver.py` + `eval_mcp/bedrock_capture.py` — in-harness OTLP receiver consumes spans from those subprocesses (env `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf`) — this is how agent evals capture Bedrock calls without code modification.
- `eval_mcp/storage.py` + `eval_mcp/core/user_storage.py` — JSON files under `~/.eval-mcp/users/<user>/` (overridable via `USER_STORAGE_BASE`).
- `eval_mcp/s3_sync.py` — optional bidirectional sync with a team S3 bucket; enabled by `eval-mcp init <bucket>`.
- `eval_mcp/viewer.py` — FastAPI viewer that serves `viewer_static/` for results.

Tool order in a typical session: `list_bedrock_models` → `generate_qa_pairs` → `generate_judge` → `create_eval_config` → `run_evaluation` → `get_viewer_url`. The agent system prompt that orchestrates this lives in `backend/core/agent.py` (used by the EKS web app) — the MCP itself doesn't host an agent; the IDE's model is the driver.

### EKS web app flow (separate from MCP)

`./deploy.sh` runs two Terraform layers with independent state:
- `infra/data/` — VPC, RDS Postgres, S3 buckets. Persistent across redeploys.
- `infra/platform/` — EKS, Karpenter, ALB, CloudFront, WAF, Cognito. Recreated by destroy/deploy.

Data-layer outputs flow into platform-layer via `-var=` flags (NOT `terraform_remote_state`, to avoid leaking secrets between states). Helm chart at `helm/eval/` deploys a stateless backend Pod (the backend FastAPI + `eval-mcp` as a K8s 1.28+ native sidecar over an emptyDir `/data`) and a frontend Pod; durable state lives in RDS + S3.

`infra/eval-logs-bucket/` is a third, unrelated Terraform root — it's the optional S3 bucket for MCP team sharing, surfaced through `eval-mcp init`. Has its own provider block and account-ID-suffixed naming.

### Adding a model

Touch both: `eval_mcp/tools/bedrock_models.py` (add to `SUPPORTED_MODELS`) AND `eval_mcp/provider_pricing.json` (per-1M-token pricing). Missing pricing entries silently break cost reporting downstream.

### Adding a tool

1. Async handler in `eval_mcp/tools/<name>.py`.
2. Register in `eval_mcp/server.py` with typed signature + tool annotation preset (`READ_LOCAL` / `CREATE_REMOTE` / etc.) — the docstring becomes the LLM-visible description.
3. Pytest for any narrow deterministic logic.
4. Exercise via Claude Code pointed at the editable install before shipping.

## Conventions worth knowing up front

- **Worktrees by default** for non-trivial changes: `git worktree add .claude/worktrees/<name> -b <type>/<name>`. Keeps `viewer_static/`, `node_modules/`, build artifacts from colliding across parallel branches. `.claude/` is gitignored except `.claude/skills/`. Skip the worktree for trivial single-file edits that'll merge in the next minute.
- **Conventional Commits** for every commit and every PR title (`feat(mcp): ...`, `fix(release): ...`). Enforced by convention, not lint.
- **Never push to `main`, never force-push, never auto-release on merge.** Releases are an explicit `make release` after the user says ship. See [ship-it skill](./.claude/skills/ship-it/SKILL.md) for the full flow + the supply-chain reasoning behind avoiding release-please-style bots.
- **`uvx` caches resolved versions per user.** A fresh PyPI release won't reach existing users until they run `uv cache clean llm-evaluation-system`. When verifying a release locally use `uvx --refresh --from 'llm-evaluation-system==X.Y.Z' eval-mcp --help`.

## Notes for AI agents

Claude Code and other agentic tools auto-summarize prior conversation turns when the context window fills up — the conversation isn't capped by the window. Don't stop work mid-task to "save context," compress your writing terser than the task requires, commit half-done changes prematurely, or suggest opening a fresh session just because the chat has gotten long. Those impulses fracture a coherent change set the user has to stitch back together. If the limit is genuinely reached, the platform handles it — focus on finishing what was asked.
