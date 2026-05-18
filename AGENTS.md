# AGENTS.md

## How it works

Users interact through a chat interface. The agent uses MCP tools in this order:
`list_bedrock_models` → `generate_qa_pairs` → `generate_judge` → `create_eval_config` → `run_evaluation` → `get_viewer_url`

## Adding a new model

Update both files:
1. `eval_mcp/tools/server_http.py` — add to `SUPPORTED_MODELS`
2. `eval_mcp/provider_pricing.json` — add pricing (per 1M tokens)

## Key files

| File | Purpose |
|------|---------|
| `backend/core/agent.py` | Agent system prompt and loop |
| `backend/core/bedrock_client.py` | Bedrock client + API key auth helper |
| `eval_mcp/server.py` | Unified MCP server (all eval tools) |
| `eval_mcp/tools/` | Eval tools (QA gen, judge, config, run, …) |
| `eval_mcp/provider_pricing.json` | Source of truth for model pricing |
| `eval_mcp/core/judge_config.py` | Default judge models and criteria |
| `Makefile` | Local dev commands (`make dev`, `make run`, `make stop`) |

## Releasing

The version lives in git tags. `pyproject.toml` has no `version` field —
`setuptools-scm` derives it from the latest `v*` tag at build time.

After merging a PR with user-visible changes, publish to PyPI from main:

```bash
git checkout main && git pull
make release         # patch bump (e.g. 0.3.5 → 0.3.6) — bug fixes only
make release-minor   # minor bump (e.g. 0.3.5 → 0.4.0) — new features, backwards-compat
make release-major   # major bump (e.g. 0.3.5 → 1.0.0) — breaking changes
```

Each target reads the latest tag, computes the next, tags it, and pushes
the tag. No source file is bumped; no "Release vX.Y.Z" commits land on
main. The `publish.yml` workflow runs on tag push and publishes to PyPI
via trusted publisher (setuptools-scm bakes the tag's version into the
built artifacts).

Pick the bump from semver: new public API = minor, only bug fixes =
patch, backwards-incompatible = major. Don't tag manually outside the
Makefile — the targets enforce clean tree + on main + up-to-date.
