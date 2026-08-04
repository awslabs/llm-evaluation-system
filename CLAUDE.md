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

### Don't pass max_tokens to evals — and don't reintroduce a lookup

**We pass NO `max_tokens` to any eval.** This is a workaround for an upstream
Inspect AI gap, and the design deliberately consults nothing external.

Inspect's Bedrock provider injects a *constant* default when the caller passes
none: `DEFAULT_MAX_TOKENS = 2048` (`inspect_ai/_util/constants.py`), via a
hand-coded family table in `_providers/bedrock.py`. On the Converse API that
overrides a **correct** AWS default — the API reference states that when
`maxTokens` is omitted, "the default value is the maximum allowed value for the
model". So Inspect turns "run to the model's limit" into "stop at 2048", and for
reasoning models (gpt-oss, GPT-5.x) that produces **empty** completions — the
whole budget goes to the reasoning channel — which score 0 while the run reports
success. Verified against Inspect `main` (2026-07-30): still 2048, still a family
table, still no truncation warning (PR #3933 proposed one, closed unmerged).

The fix is `eval_mcp/inspect_patches.py`: it makes the Bedrock provider's
`max_tokens()` return `None`, so Inspect sends nothing and Bedrock applies the
model's own default. It's loaded inside the eval subprocess by
`eval_mcp/_inspect_main.py` — every launch path (`run_eval`, `optimize_prompt`,
`benchmarks`, retries) goes through `_INSPECT_CMD = [..., "-m",
"eval_mcp._inspect_main"]` rather than `-m inspect_ai`, so the patch lands in the
process that actually calls the model. Generated task files also `import
eval_mcp.inspect_patches` directly, so a config run by hand still gets it.

**Why not a per-model lookup** (an earlier version of this did exactly that,
reading `max_output_tokens` from LiteLLM — don't bring it back): verified against
live Bedrock, LiteLLM's advertised limits are wrong for **35 of 38** on-demand
models. Too low for 29 (mostly defaulting to 8192 — the same constant, laundered
through a lookup), and dangerously too high for one (it lists qwen3-235b at
131072 when Bedrock's real max is 65536). Exceeding a limit is a hard
`ValidationException`, not a clamp, so a wrong-high number kills every sample.

**Why not a single constant:** no value works for all. `8192` is rejected by
`writer.palmyra-vision-7b` (max 4096) while being too low for gpt-oss. Omitting
is the only option that never crashes and needs no data.

**Omitting is not perfect, and that's expected.** Bedrock's own omitted default
is itself sometimes below a model's true max — gpt-oss-20b defaults to 4096
though it accepts 8192+. Omitting is strictly better than 2048 and never
crashes; the residual truncation is *surfaced*, not guessed around: the jury
scorer flags `truncated_no_output` (empty completion → scored 0 with a TRUNCATED
explanation) and `truncated_partial_output` (answer cut off mid-stream → still
scored, since partial output carries signal, but marked because completeness
criteria necessarily fail on a severed answer).

When upstream stops substituting a constant (or Bedrock starts clamping instead
of rejecting), delete `inspect_patches.py` + `_inspect_main.py`, point
`_INSPECT_CMD` back at `-m inspect_ai`, and go back to plain `generate()`.

**Mantle frontier models are Responses-API-only.** AWS's docs recommend
`/v1/chat/completions`, but GPT-5.6 rejects it: *"The model
'openai.gpt-5.6-terra' does not support the '/v1/chat/completions' API"*. Only
`/openai/v1/responses` works for inference; the catalog stays at `/v1/models`.

### Two kinds of benchmark — don't merge the two tools

There are two separate benchmark paths, and they are not interchangeable:

- **`eval_mcp/tools/benchmarks.py`** (`list_benchmarks` / `run_benchmark`) is a
  thin wrapper over the installed **`inspect_evals`** catalog (~130 single-turn
  Q&A benchmarks: MMLU, GPQA, HumanEval…). It runs `inspect_evals/<task>` by
  registry name, so it can only run what that package registers.
- **`eval_mcp/benchmarks/`** (`list_multiturn_benchmarks` /
  `run_multiturn_benchmark`) holds benchmarks we **vendor and run ourselves**,
  for shapes `inspect_evals` doesn't cover. Currently `aiwf_medium_context` and
  `aiwf_long_context` — a 30-turn scripted conversation with 5 tools, ported
  from [kwindla/aiewf-eval](https://github.com/kwindla/aiewf-eval) (MIT).
  Launched by **absolute path** (`<task_file>@<task_name>`), which works from
  any cwd.

**Why not add new benchmarks to `inspect_evals` instead:** as of 2026-05-08 it
stopped accepting new eval code (its `EVAL_REGISTER.md` — a dependency-isolation
decision, not a quality one). New evals now live in the author's own repo and get
*listed* upstream via the register. Register entries are **not shipped in the
`inspect_evals` wheel** (`load_listing()` resolves the register dir as
`package_dir.parent.parent / "register"`, a source-checkout path, and packaging
only includes `inspect_evals/*/eval.yaml`), so nothing in the register is
runnable through `run_benchmark` — verified: `internal: 129, external: 0` on the
installed wheel. Bundling here is the only path that actually runs.

Two things to know before touching `eval_mcp/benchmarks/aiwf/`:

- **Multi-turn scoring is per-TURN, not per-sample.** One sample *is* the whole
  conversation, so the metrics (`pass_rate`, `tool_use_rate`, …) are custom
  `@metric`s that pool turn counters out of score metadata. `accuracy()` cannot
  express this. Each rate needs its own decorated function — `@metric` fixes the
  display name at decoration time, so one parameterised factory reports four
  metrics all called `turn_metric`.
- **Two upstream behaviours are load-bearing; don't "simplify" them away.**
  (1) A turn *ends at the tool call* — no second generate after the tool result
  (upstream's `default_tool_result_run_llm = False`); generating again hands the
  model a free retry. (2) The **recovery nudge**: when a turn expected a tool
  call and none came, upstream injects one synthetic `"Please go ahead."` turn —
  and does **not** score that attempt (its judge skips `recovery_turn` records).
  The nudge unblocks the conversation; it earns the turn no credit. Merging it
  into the turn, as an earlier version did, lets a model that only complied when
  prodded score as if it complied immediately.
- **The judge prompt is upstream's verbatim, not a paraphrase.** It's vendored at
  `data/upstream_judge_system_prompt.txt` and the audio-only `turn_taking`
  dimension is stripped programmatically at load time, with a test diffing the
  result against the original so nothing else can drift. Rewriting the rubric for
  readability silently changes what the benchmark measures — that already
  happened once. Full fidelity notes, and what was deliberately not ported (all
  speech-to-speech), in `eval_mcp/benchmarks/aiwf/NOTICE.md`.

### Adding a model

Nothing to do. There is no allowlist and no hand-maintained price table — a newly
launched Bedrock model surfaces automatically the moment AWS enables it on the
account (`list_bedrock_models` returns everything text-capable and ON_DEMAND-invokable),
and its pricing resolves live from LiteLLM (`eval_mcp/core/pricing.py`). Compatibility
is gated at run time by the Converse smoke test in `run_eval.validate_providers`, which
fails fast with an actionable message if a chosen model doesn't work with the eval
pipeline. If you ever need the offline fallback prices to be more current, run
`make sync-pricing` and review the diff.

The same applies to the OpenAI frontier models on **Bedrock Mantle**
(`openai/bedrock/<id>` — GPT-5.x, served on a separate OpenAI-compatible endpoint,
NOT Converse, so they never appear in `list_foundation_models`). Those come from
Mantle's live `/v1/models` catalog via `external_providers.list_mantle_models()`
(10-min cache); the list in `EXTERNAL_PROVIDERS["bedrock-mantle"]` is only an
offline fallback.

**Region matters for these.** Mantle models launch region-by-region, so
availability is per-region, not global — as of 2026-07-27 `gpt-5.5` and
`gpt-5.6-sol` are us-east-1/us-east-2 only, while `gpt-5.6-terra`, `gpt-5.6-luna`
and `gpt-5.4` are also in us-west-2. Consequences worth knowing:

- Region resolution goes through `bedrock_client.resolve_region()`:
  `AWS_REGION` → `AWS_DEFAULT_REGION` → **the resolved profile's region** →
  `DEFAULT_REGION` (**us-east-2**). Never re-add a bare
  `os.environ.get("AWS_REGION", ...)` for a Bedrock call — that ignores the
  user's profile and silently hides the us-east-only models.
- **`DEFAULT_REGION` is us-east-2, not us-west-2**, because that's the region
  carrying the full model set: the Mantle frontier models are us-east-only, and
  every current-generation Converse model (Claude Opus 5 / Sonnet 5 / Haiku 4.5 /
  Sonnet 4.6, Nova, gpt-oss) is in us-east-2 too. Pinned by a test — don't
  "tidy" it back to us-west-2.
- **There is exactly one default region, and it is Bedrock-only.**
  `core/user_storage.py`, `core/s3_client.py` and `backend/core/database.py`
  read `AWS_REGION` with **no fallback at all** (empty string) and must NOT use
  `resolve_region()`. They address S3 buckets and RDS whose location is fixed
  when created; the Bedrock region would point at resources that don't exist
  there, and a guessed default would mean silently hitting the wrong bucket or
  an opaque IAM-auth failure instead of an honest error. A bucket/database is
  only ever configured together with an explicit `AWS_REGION` (both come from
  Helm/Terraform), and with no bucket set those modules never touch S3.
- **Mantle models are auto-routed cross-region**, so GPT-5.x works for any user
  regardless of where they are. Credentials are global and only the endpoint is
  regional, so `external_providers.resolve_mantle_region()` sends a model the
  caller's region doesn't serve to one that does; the decision is baked into the
  config's `mantle_regions` map at creation time. Judges apply it per-model in
  the generated task file; targets arrive via `--model` on the CLI, so
  `run_eval._region_for_run()` moves the whole subprocess instead — the two
  alternatives are dead ends (`-M aws_region=` is global and errors on Converse
  models; `BEDROCK_OPENAI_BASE_URL` alone gives "Credential should be scoped to
  a valid region" because the bearer token is still minted for the ambient
  region). Only `openai/bedrock/*` inference moves — storage, logs and Converse
  models stay put. `EVAL_MCP_MANTLE_REGION` pins a region;
  `EVAL_MCP_NO_CROSS_REGION=1` disables the hop for data-residency constraints.
- Before concluding a model doesn't exist, check another region. `list_*` output
  is region-scoped, and a "not available" from one region is not evidence of
  absence. `run_eval` also reports this: a Mantle validation failure probes the
  other regions and names the ones that do serve the model.

### The mcp SDK is 2.x — things not to "tidy" back

`eval_mcp/server.py` is built on `mcp.server.MCPServer` (mcp >= 2.0). v1's
`FastMCP` no longer exists, and `pyproject.toml` floors mcp at `>=2.0.0`
(pinned by a test). Four consequences that look like mistakes but aren't:

- **`port`/`host` are not constructor args.** 2.x moved every
  transport-specific parameter onto the run/app methods. The HTTP branch in
  `main()` passes them to its own `uvicorn.run(...)`, which is why the
  constructor doesn't need them. Re-adding `MCPServer(..., port=)` is a
  `TypeError`.
- **`version=` is passed explicitly.** v1 derived it; 2.x defaults to `""`
  and would report a blank `serverInfo.version` to every client. It comes
  from installed package metadata because setuptools-scm owns the version.
- **The 4 MiB Streamable HTTP body cap is kept.** 2.x added it (v1 had
  none), and nothing here sends a large body: the web app truncates to ~11
  rows before `analyze_dataset` and writes datasets in-process via
  `save_dataset_to_db`, so files never cross the wire. If a client ever
  needs more, pass `max_request_body_size=` to `streamable_http_app()`
  rather than dropping the bound.
- **Annotations are camelCase in constructors, snake_case on attributes.**
  `ToolAnnotations(readOnlyHint=True)` is still correct, but *reading* it is
  `.read_only_hint`. The wire format stays camelCase, so clients are
  unaffected.

Client-side, the Streamable HTTP transport is `streamable_http_client`
(renamed from `streamablehttp_client`). Note mcp 2.x depends on **`httpx2`**,
a separately-named package; our own code still uses plain `httpx` and the two
coexist as distinct modules.

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
