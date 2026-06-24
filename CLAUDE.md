# CLAUDE.md

This is the canonical agent-facing doc for the repo — Claude Code reads it directly, and [`AGENTS.md`](./AGENTS.md) is a thin pointer back here so other tools that follow the [agents.md](https://agents.md) convention (Codex, Cursor) land in the same place.

## What's in this repo

Two deployables that share some code:

- **`eval_mcp/`** — the MCP package published to PyPI as `llm-evaluation-system` (entry point `eval-mcp`). Self-contained, no database, no web app. This is what 99% of users install.
- **`backend/` + `frontend/`** — the optional EKS web app (FastAPI chat + Vite/React UI + Cognito auth). `./deploy.sh` is its entry point; `make dev` runs it locally via Docker Compose.

`frontend/` is a single Vite + React SPA (client-side routing via react-router). `vite build` produces a static bundle served two ways: bundled into `eval_mcp/viewer_static/` for the MCP's local results viewer (`npm run build:viewer`), and served from a **private S3 bucket via CloudFront OAC** for the EKS web deployment (no frontend pod — see [ARCHITECTURE.md](./ARCHITECTURE.md)). Locally, nginx serves the bundle and proxies the gated paths (`/api`, `/inspect`) to the backend, mirroring that CloudFront/S3 split. Changing frontend code therefore affects the PyPI wheel — the viewer static is package data per `pyproject.toml`.

## Key files

| File | Purpose |
|------|---------|
| `eval_mcp/server.py` | Unified MCP server — every tool is registered here |
| `eval_mcp/tools/` | Tool handlers (QA gen, judge, config, run, …) |
| `eval_mcp/core/bedrock_client.py` | Bedrock client + cross-region inference + API-key auth |
| `eval_mcp/core/judge_config.py` | Default judge models and criteria |
| `eval_mcp/core/pricing.py` | Live model pricing from LiteLLM (24h cache → vendored snapshot fallback); no hand-maintained price table |
| `eval_mcp/core/litellm_pricing_snapshot.json` | Vendored offline fallback for pricing; refresh with `make sync-pricing` |
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
uv pip install -e ".[backend]"                         # one-time: full-suite deps (asyncpg)
.venv/bin/pytest tests/                                # full suite
.venv/bin/pytest tests/test_run_eval.py                # one file
.venv/bin/pytest tests/test_run_eval.py::test_name     # one test
.venv/bin/pytest -k "qa_allocation"                    # by keyword
```

The full suite needs the `[backend]` extra (`asyncpg`) for the web-app `test_data_api`
tests — without it those error at collection, which also takes down unrelated tests in
the same session. `inspect_evals` is a core dep (used by `test_benchmarks`); if it's
missing, `uv pip install -e .` again to resync.

Pytest is **only useful for narrow deterministic logic** (parsing, validation, regex). End-to-end coverage requires running the MCP from Claude Code — mocks of Bedrock/subprocesses/user-dirs produce false greens. See `docs/DEVELOPMENT.md` section 2.

**Testing the web app for real** — pytest greens don't prove the web app works; exercise it against the live `make dev` stack in layers: (1) `pytest` for pure logic; (2) `curl`/`urllib` against `:4001` for `/api/*` routes (the `verify_*.py` scripts are the template); (3) **chat/agent behavior → POST `/api/chat/message` with `{"stream":false}` and assert on the reply** — this is how you prove the model actually invokes an MCP tool, and it needs Bedrock creds (which `make dev` already exports, so it's never a blocker); (4) **browser UI → the `webapp-testing` skill (Playwright)**. Don't conflate (3) and (4): chat behavior is curl-the-chat-endpoint, the browser skill is for what renders. Full methodology + the two-identity trick in `docs/DEVELOPMENT.md` ("How to *really* test the web app").

### Frontend / viewer

```bash
cd frontend
npm install                  # first time only
npm run build:viewer         # vite build + copy to eval_mcp/viewer_static/
npm run dev                  # Vite dev server on :5173 (proxies /api → backend)
npm run lint                 # eslint
```

`build:viewer` runs `vite build` and replaces `eval_mcp/viewer_static/` with the static bundle (`index.html` + `assets/`). Run it whenever you change frontend source if you want the local MCP viewer to reflect it. The Vite dev server proxies `/api` and `/inspect` to a backend (set `BACKEND_URL`, default `http://localhost:8000`); point it at `eval-mcp view` (:4001) for viewer work or the full backend (:8000) for chat.

### Local full-stack (web app)

```bash
AWS_PROFILE=my-profile make dev          # build SPA + docker compose (backend hot-reloads)
make dev-spa                              # rebuild just the SPA bundle (nginx picks it up on refresh)
make logs s=backend                       # tail one service
make restart s=backend                    # restart one with fresh creds
make stop                                 # docker compose down
make clean                                # also wipe volumes
```

`make dev` builds the static SPA into `frontend/dist`, which nginx serves directly while proxying the gated paths to the backend (no Node frontend container — nginx stands in for CloudFront+S3 serving static and the ALB/oauth2-proxy gating the API, mirroring the EKS split). The backend hot-reloads on Python edits. For frontend edits, rerun `make dev-spa` and refresh. Open http://127.0.0.1:4001. See [local/README.md](local/README.md).

### Deploy-then-merge (web app changes)

**Always deploy the feature branch to AWS and verify live BEFORE merging — never merge then deploy.** `./deploy.sh` zips the local working tree, so a deploy from inside the feature-branch worktree ships exactly that branch's code to EKS (us-east-2). The order is:

1. Make the change on a feature-branch worktree, test locally (`pytest` + a real eval where it applies).
2. **Deploy that worktree** to us-east-2 (`AWS_REGION=us-east-2 AWS_PROFILE=<profile> ./deploy.sh`) and verify against the live pod / chat endpoint — this is the only way to catch deployed-environment bugs (sandbox availability, IAM grants, dependency-resolution drift, agent tool-selection) that local tests and pytest greens miss.
3. Only after prod verification passes: open/merge the PR.

Rationale: several real bugs in this project only appeared in the deployed environment (openai version floor rejected at runtime, Mantle IAM, provider-filter excluding models, benchmark Docker-sandbox unavailable on k8s). Merging first means shipping unverified code to `main`; deploying the branch first lets us prove it in prod and merge with confidence. The deploy is non-destructive (idempotent DDL, rolling backend restart), so deploying an unmerged branch is safe.

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
- `infra/data/` — VPC, RDS Postgres, S3 buckets (incl. the private SPA bucket), and the **Cognito user pool**. Persistent across redeploys (so `destroy.sh` preserves user accounts + data).
- `infra/platform/` — EKS, Karpenter, ALB, CloudFront, WAF, the Cognito **client** + SPA OAC/origin/function. Recreated by destroy/deploy.

Data-layer outputs flow into platform-layer via `-var=` flags (NOT `terraform_remote_state`, to avoid leaking secrets between states). Helm chart at `helm/eval/` deploys a single stateless backend Pod (the backend FastAPI + `eval-mcp` as a K8s 1.28+ native sidecar over an emptyDir `/data`) plus oauth2-proxy — **there is no frontend pod**; the SPA is served from S3 via CloudFront OAC. Durable state lives in RDS + S3 + the Cognito pool. SPA publish (`npm run build` → `aws s3 sync` → CloudFront invalidation) happens in `buildspec-scripts/deploy.sh`.

`infra/eval-logs-bucket/` is a third, unrelated Terraform root — it's the optional S3 bucket for MCP team sharing, surfaced through `eval-mcp init`. Has its own provider block and account-ID-suffixed naming.

### Adding a model

Nothing to do. There is no allowlist and no hand-maintained price table — a newly
launched Bedrock model surfaces automatically the moment AWS enables it on the
account (`list_bedrock_models` returns everything text-capable and ON_DEMAND-invokable),
and its pricing resolves live from LiteLLM (`eval_mcp/core/pricing.py`). Compatibility
is gated at run time by the Converse smoke test in `run_eval.validate_providers`, which
fails fast with an actionable message if a chosen model doesn't work with the eval
pipeline. If you ever need the offline fallback prices to be more current, run
`make sync-pricing` and review the diff.

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

## Skills worth invoking (Claude Code only)

These [marketplace skills](https://code.claude.com/docs/en/skills) from the official Anthropic marketplace pair well with this repo's workflows. They're user-installed (not bundled here), so the recommendation only fires for sessions where the user has them available — but if you do, lean on them rather than reinventing the wheel.

- **`webapp-testing`** — after any change the viewer renders, including: UI/routing edits under `frontend/` (full web app on `:4001` via `make dev`, Vite dev server on `:5173`, or the bundled viewer on `:4001`), and backend edits that change the JSON shape the viewer consumes (e.g. `eval_mcp/core/eval_results.py`, `eval_mcp/viewer.py`, `list_evaluations`). Spins up Playwright and actually clicks through pages. A label or column-header change in a Python file is still a UI change — verify it in the browser, don't just inspect the JSON.
- **`frontend-design`** — when adding or restyling components in `frontend/`. Same source builds both the web app and the static viewer export, so component quality lands in both deliverables.
