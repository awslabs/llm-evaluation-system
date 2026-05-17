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

Releases are automated by [release-please](https://github.com/googleapis/release-please).
You do not run `make release` in normal flow — the bot does it for you.

**Normal flow:**

1. Open a PR with a Conventional Commits title (`feat: ...`, `fix: ...`, `docs: ...`).
   The PR title lint blocks merge if the prefix is missing.
2. Merge the PR to main.
3. `release-please` reads commits since the last release tag and, if any
   `feat:` / `fix:` are present, opens (or updates) a "Release PR"
   titled `chore(main): release X.Y.Z` with an auto-generated
   `CHANGELOG.md` entry and a new version computed via semver:
   - `feat:` → minor bump
   - `fix:` / `perf:` → patch bump
   - any `BREAKING CHANGE:` footer → major bump
4. Merge the Release PR when you're ready to ship. The bot creates the
   `vX.Y.Z` tag, which triggers `publish.yml` → build → PyPI upload.

The version itself lives in git tags. `pyproject.toml` has no `version`
field — `setuptools-scm` derives it from the tag at build time. The
`.release-please-manifest.json` file tracks the current released
version for release-please's own bookkeeping; you don't edit it by hand.

**Manual escape hatch (rare):**

If you ever need to release out of band — e.g. a security fix that
shouldn't wait for the next Release PR cycle — `make release` /
`make release-minor` / `make release-major` still work. They read the
latest tag, compute the next version, tag, and push. Use only when the
release-please flow can't be used.

Don't tag manually outside the Makefile — the targets enforce clean
tree + on main + up-to-date.
