# AGENTS.md

## How it works

Users interact through a chat interface. The agent uses MCP tools in this order:
`list_bedrock_models` → `generate_qa_pairs` → `generate_judge` → `create_eval_config` → `run_evaluation` → `get_viewer_url`

## Adding a new model

Update both files:
1. `backend/mcp_servers/providers/server_http.py` — add to `PROMPTFOO_SUPPORTED_MODELS`
2. `backend/core/bedrock_pricing.json` — add pricing (per 1M tokens)

## Key files

| File | Purpose |
|------|---------|
| `backend/core/agent.py` | Agent system prompt and loop |
| `backend/core/bedrock_client.py` | Bedrock client + API key auth helper |
| `backend/core/bedrock_pricing.json` | Single source of truth for model pricing |
| `backend/core/judge_config.py` | Default judge models and criteria |
| `backend/mcp_servers/providers/server_http.py` | Model allowlist and discovery |
| `backend/mcp_servers/synthetic/server_http.py` | Eval tools (QA gen, judge, config, run) |
| `Makefile` | Local dev commands (`make dev`, `make run`, `make stop`) |
