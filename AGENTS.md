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

## Starting new work

Default to a **git worktree** under `.claude/worktrees/<branch-name>/`
for any non-trivial change, rather than checking out a branch in the
main repo directory. This keeps parallel sessions and branches isolated
(build artifacts, viewer_static, node_modules don't collide). The
`.claude/` directory is gitignored except for `.claude/skills/`.

```bash
git worktree add .claude/worktrees/<name> -b <type>/<name>
cd .claude/worktrees/<name>
```

Skip the worktree only for trivial single-file edits you'll merge in the
next minute.

## Shipping changes (commit → PR → release)

Use the **[`ship-it` skill](./.claude/skills/ship-it/SKILL.md)** — it
encodes this repo's conventions for conventional-commit titles, PR flow,
manual `make release` against `setuptools-scm` tags, and post-merge
cleanup. Don't invent an ad-hoc git workflow; invoke the skill so the
conventions stay consistent. The skill auto-loads via Claude Code's
`.claude/skills/` mechanism and triggers when the user wants to commit,
push, open a PR, or release.
