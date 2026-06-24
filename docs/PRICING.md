# Model pricing & discovery

This system has **no hand-maintained model list and no hand-maintained price
table**. A newly launched model (e.g. a future Opus 5) works end-to-end the
moment AWS enables it on the account — discovery, validation, and pricing all
resolve automatically.

## Discovery — no allowlist

`eval_mcp/tools/bedrock_models.py` returns every model AWS reports as available:

- `list_inference_profiles` (paginated) — cross-region endpoints (`us.`, `eu.`, …).
- `list_foundation_models` — direct models, filtered to those that are
  `ON_DEMAND`-invokable and text-output (using AWS's own `inferenceTypesSupported`
  / `outputModalities` metadata), so we never surface an un-callable base ID.

There used to be a curated `SUPPORTED_MODELS` set. It was removed: the run-time
Converse smoke test below is a *stronger* compatibility gate than a static list,
and it requires zero maintenance.

## Validation — run-time smoke test

`run_eval.validate_providers` issues a real `converse()` call (`maxTokens` tiny)
against each chosen model before an eval starts. If a model doesn't support the
Converse API or isn't enabled, the eval fails fast with an actionable message
("Model not enabled in AWS account", "Invalid model ID", …) instead of the model
being silently hidden from discovery. This is where compatibility is enforced.

## Pricing — live from LiteLLM

`eval_mcp/core/pricing.py` resolves prices in three tiers, freshest first:

1. **Live** — fetches LiteLLM's `model_prices_and_context_window.json` from raw
   GitHub, written to a 24h on-disk cache (`~/.eval-mcp/pricing_cache.json`).
2. **Cache** — the last successful fetch, used when offline or the fetch fails.
3. **Vendored snapshot** — `eval_mcp/core/litellm_pricing_snapshot.json`, shipped
   in the package so cost reporting never hard-breaks for air-gapped users.

The fetch happens at most once per process, off the hot path, and never blocks an
eval — any failure silently degrades to the cache or snapshot.

### Why LiteLLM, and why it's safe

LiteLLM's dataset is the de-facto community source for LLM pricing (MIT licensed,
~2,900 models incl. ~330 Bedrock entries with cross-region uplift and cache-tier
pricing). We consume **only the JSON data file** — we do *not* install the
`litellm` package. The data is pure numbers and strings: no code is ever fetched
or executed, so the worst-case failure is a *wrong price*, never code execution.

Region-aware matching honors cross-region uplift (e.g. `us.anthropic.claude-...`
costs more than the base model) and cache read/write tiers, both of which the old
hand-maintained table got wrong.

### Model-ID matching

LiteLLM keys Bedrock models by their bare ID (`us.anthropic.claude-…` or
`anthropic.claude-…`), not with a `bedrock/` prefix. `_candidates()` tries, in
order: the raw string → provider-prefix stripped (keeps region prefix for uplift)
→ region-prefix stripped (base price) → progressive `-` suffix trims. Unknown
models return `None` (distinct from a `$0` cost) so callers can show "pricing
unavailable" rather than a misleading zero.

## Keeping the offline snapshot current

```bash
make sync-pricing            # refresh eval_mcp/core/litellm_pricing_snapshot.json
python scripts/sync-pricing.py --dry-run   # preview the diff, write nothing
```

Review the `git diff` before committing — this is the human-in-the-loop gate that
keeps an upstream change from silently altering reported costs. Live users already
get current prices via tier 1/2; the snapshot only matters for offline fallback.

### Pinning (optional)

For reproducible/audited environments, pin the source via env:

| Env var | Effect |
|---|---|
| `EVAL_MCP_PRICING_URL` | Override the upstream URL (e.g. a pinned commit SHA, or a self-hosted mirror) |
| `EVAL_MCP_PRICING_OFFLINE=1` | Never touch the network; use cache + snapshot only |
| `EVAL_MCP_PRICING_TTL` | Cache freshness in seconds (default 86400) |
| `EVAL_MCP_PRICING_TIMEOUT` | Live-fetch timeout in seconds (default 8) |
