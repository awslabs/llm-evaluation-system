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

After merging a PR with user-visible changes, publish to PyPI from main:

```bash
git checkout main && git pull
make release         # patch bump (0.3.0 → 0.3.1) — bug fixes only
make release-minor   # minor bump (0.3.0 → 0.4.0) — new features, backwards-compat
make release-major   # major bump (0.3.0 → 1.0.0) — breaking changes
```

Each target bumps `pyproject.toml`, commits, tags `vX.Y.Z`, and pushes.
The `publish.yml` GitHub workflow takes over from the tag push and
publishes to PyPI via trusted publisher. Don't tag manually.

Pick the bump from semver: new public API = minor, only bug fixes =
patch, backwards-incompatible = major.
