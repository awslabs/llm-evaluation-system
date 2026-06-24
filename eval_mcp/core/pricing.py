"""Model pricing — live from LiteLLM, cached locally, with a vendored fallback.

Prices are in $/million tokens. Cost for a call is::

    (input_tokens  * input_price
     + output_tokens * output_price
     + cache_write_tokens * input_cache_write_price
     + cache_read_tokens  * input_cache_read_price) / 1_000_000

Why this design (see docs/PRICING.md): we do NOT hand-maintain a price table.
A new model (e.g. a future Opus 5) is priced automatically the moment it
appears in the community-maintained LiteLLM dataset. The data is *pure JSON
numbers* — no code is ever fetched or executed — so the only failure mode is a
wrong price, never code execution.

Three-tier resolution, most-fresh first:
  1. Live fetch of LiteLLM's ``model_prices_and_context_window.json`` from raw
     GitHub (pinned-able via env), written to a local 24h cache.
  2. The local cache, if the live fetch fails or we're offline.
  3. A vendored snapshot shipped in the package, so cost reporting never hard
     breaks for air-gapped users.

The network fetch happens at most once per process (and once per 24h on disk),
off the hot path, and never blocks an eval — a failure silently degrades to the
cache or the snapshot.
"""

import json
import os
import re
import time
import urllib.request
from pathlib import Path
from typing import Optional

# Source of truth: LiteLLM's pricing dataset (MIT licensed, pure data).
# Override the ref or URL via env for pinning / self-hosting / testing.
_LITELLM_URL = os.environ.get(
    "EVAL_MCP_PRICING_URL",
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json",
)

# Vendored fallback snapshot (refreshed via `make sync-pricing`).
_SNAPSHOT_FILE = Path(__file__).parent / "litellm_pricing_snapshot.json"

# Local cache of the last successful live fetch.
_CACHE_FILE = Path(
    os.environ.get("EVAL_MCP_HOME", str(Path.home() / ".eval-mcp"))
) / "pricing_cache.json"

# How long a cached live fetch stays fresh before we try the network again.
_CACHE_TTL_SECONDS = int(os.environ.get("EVAL_MCP_PRICING_TTL", str(24 * 3600)))

# Network timeout for the live fetch — short, since we have a fallback.
_FETCH_TIMEOUT = float(os.environ.get("EVAL_MCP_PRICING_TIMEOUT", "8"))

# Disable the network entirely (air-gapped / tests): use cache+snapshot only.
_OFFLINE = os.environ.get("EVAL_MCP_PRICING_OFFLINE", "").lower() in ("1", "true", "yes")

# Process-level memo so we hit the disk/network at most once per run.
_pricing_data: Optional[dict] = None


# ---------------------------------------------------------------------------
# Data loading (live -> cache -> snapshot)
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _cache_is_fresh() -> bool:
    try:
        return (time.time() - _CACHE_FILE.stat().st_mtime) < _CACHE_TTL_SECONDS
    except OSError:
        return False


def _fetch_live() -> Optional[dict]:
    if _OFFLINE:
        return None
    try:
        req = urllib.request.Request(
            _LITELLM_URL, headers={"User-Agent": "eval-mcp-pricing"}
        )
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if not isinstance(data, dict) or len(data) < 50:
            return None  # sanity check: a real dataset has thousands of entries
        # Best-effort cache write; never fail the call on a write error.
        try:
            _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            _CACHE_FILE.write_text(json.dumps(data))
        except OSError:
            pass
        return data
    except Exception:
        return None


def _load_pricing() -> dict:
    """Resolve the pricing dataset, freshest source first. Memoized per process."""
    global _pricing_data
    if _pricing_data is not None:
        return _pricing_data

    # 1. Fresh cache wins without touching the network.
    if _cache_is_fresh():
        cached = _read_json(_CACHE_FILE)
        if cached:
            _pricing_data = cached
            return _pricing_data

    # 2. Try live (refreshes the cache as a side effect).
    live = _fetch_live()
    if live:
        _pricing_data = live
        return _pricing_data

    # 3. Stale cache beats the snapshot (it was real upstream data once).
    cached = _read_json(_CACHE_FILE)
    if cached:
        _pricing_data = cached
        return _pricing_data

    # 4. Vendored snapshot — last resort, always present.
    _pricing_data = _read_json(_SNAPSHOT_FILE) or {}
    return _pricing_data


def refresh() -> dict:
    """Force a live refresh, bypassing the process memo and cache freshness.

    Used by the `make sync-pricing` tooling. Returns the dataset (may fall back
    to cache/snapshot if the network is unavailable).
    """
    global _pricing_data
    _pricing_data = None
    live = _fetch_live()
    if live:
        _pricing_data = live
        return live
    return _load_pricing()


# ---------------------------------------------------------------------------
# Model-ID matching
# ---------------------------------------------------------------------------

# LiteLLM keys cost as per-*token* floats; we report per-*million*.
_PER_MILLION = 1_000_000

# OTel GenAI semconv emits "aws.bedrock"; Inspect emits "bedrock".
_PROVIDER_PREFIXES = ("bedrock/", "aws.bedrock/", "openai/", "anthropic/", "google/")

_REGION_PREFIXES = ("us.", "eu.", "apac.", "global.", "us-gov.", "au.", "jp.", "ca.")


def _candidates(model_id: str) -> list[str]:
    """Ordered list of keys to try against the LiteLLM dataset.

    LiteLLM keys Bedrock models by their bare ID (``us.anthropic.claude-...`` or
    ``anthropic.claude-...``), NOT with a ``bedrock/`` prefix. We try, in order:

      1. the raw string,
      2. with any ``<provider>/`` prefix stripped (keeps the region prefix so
         cross-region uplift pricing is honored, e.g. ``us.anthropic...``),
      3. with the region prefix also stripped (base-model price),
      4. progressively shorter ``-`` suffix trims (handles ``-instruct`` etc.).
    """
    out: list[str] = [model_id]

    # Bedrock Mantle (OpenAI frontier models): Inspect uses "openai/bedrock/<id>",
    # LiteLLM keys it as "bedrock_mantle/openai.<id>" with DISTINCT pricing from
    # the OpenAI-direct API (e.g. gpt-5.4 Mantle is $2.75/$16.5 vs $2.5/$15
    # direct), so we must match the Mantle key — not fall through to bare gpt-5.4.
    if model_id.startswith("openai/bedrock/"):
        mantle_id = model_id[len("openai/bedrock/"):]
        if not mantle_id.startswith("openai."):
            mantle_id = f"openai.{mantle_id}"
        out.append(f"bedrock_mantle/{mantle_id}")

    bare = model_id
    for pfx in _PROVIDER_PREFIXES:
        if bare.startswith(pfx):
            bare = bare[len(pfx):]
            break
    if bare != model_id:
        out.append(bare)

    no_region = bare
    for pfx in _REGION_PREFIXES:
        if no_region.startswith(pfx):
            no_region = no_region[len(pfx):]
            break
    if no_region != bare:
        out.append(no_region)

    # Drop a trailing context-window qualifier like ":128k" / ":200k" that AWS
    # appends to some model IDs but LiteLLM keys without (e.g.
    # "...llama3-3-70b-instruct-v1:0:128k" -> "...-v1:0").
    for form in (bare, no_region):
        m = re.match(r"^(.*-v\d+:\d+):\w+$", form)
        if m:
            out.append(m.group(1))

    # Suffix trims on whichever bare form we ended up with (shorter -> shorter).
    for base in (bare, no_region):
        trimmed = base
        while "-" in trimmed:
            trimmed = trimmed.rsplit("-", 1)[0]
            out.append(trimmed)

    # De-dup, preserve order.
    seen = set()
    uniq = []
    for c in out:
        if c and c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq


def _lookup(model_id: str) -> Optional[dict]:
    """Return a LiteLLM entry dict for the best-matching key, or None."""
    data = _load_pricing()
    for key in _candidates(model_id):
        entry = data.get(key)
        if isinstance(entry, dict) and entry.get("input_cost_per_token") is not None:
            return entry
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_model_cost(model_id: str) -> Optional[dict]:
    """Return per-million-token pricing for a model, or None if unknown.

    Shape matches Inspect AI's ``ModelCost``::

        {"input": float, "output": float,
         "input_cache_write": float, "input_cache_read": float}
    """
    entry = _lookup(model_id)
    if entry is None:
        return None

    def pm(field: str) -> float:
        v = entry.get(field)
        return round(v * _PER_MILLION, 6) if isinstance(v, (int, float)) else 0.0

    return {
        "input": pm("input_cost_per_token"),
        "output": pm("output_cost_per_token"),
        "input_cache_write": pm("cache_creation_input_token_cost"),
        "input_cache_read": pm("cache_read_input_token_cost"),
    }


def calculate_cost(
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    cache_write_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> Optional[float]:
    """Calculate cost in dollars for a model call.

    Returns None if the model is not found in the pricing dataset (so callers
    can distinguish "free" from "unknown"). Cache token args are optional and
    default to 0 for backward compatibility with existing call sites.
    """
    cost = get_model_cost(model_id)
    if cost is None:
        return None
    total = (
        input_tokens * cost["input"]
        + output_tokens * cost["output"]
        + cache_write_tokens * cost["input_cache_write"]
        + cache_read_tokens * cost["input_cache_read"]
    )
    return total / _PER_MILLION
