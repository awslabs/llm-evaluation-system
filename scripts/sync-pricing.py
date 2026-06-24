#!/usr/bin/env python3
"""Refresh the vendored LiteLLM pricing snapshot.

The MCP fetches pricing live at runtime (LiteLLM raw JSON -> 24h cache), but we
also vendor a trimmed snapshot as an offline fallback. This script refreshes
that snapshot from upstream so air-gapped users get reasonably current prices.

Usage:
    python scripts/sync-pricing.py            # refresh the snapshot in place
    python scripts/sync-pricing.py --dry-run  # show the diff, write nothing

Review the git diff before committing — this is the human-in-the-loop gate that
keeps an upstream change from silently altering reported costs. The data is pure
JSON numbers (no code), so the only risk is a wrong price, not code execution.
"""

import argparse
import json
import sys
import urllib.request
from pathlib import Path

UPSTREAM_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)
SNAPSHOT = (
    Path(__file__).resolve().parent.parent
    / "eval_mcp"
    / "core"
    / "litellm_pricing_snapshot.json"
)

# Only chat/text/completion models carry the token pricing we use; drop
# image/audio/rerank/embedding rows to keep the vendored file lean.
KEEP_MODES = {"chat", "completion", "responses", None}

# Fields the runtime reshape reads — keep only these to shrink the file.
KEEP_FIELDS = (
    "input_cost_per_token",
    "output_cost_per_token",
    "cache_creation_input_token_cost",
    "cache_read_input_token_cost",
    "litellm_provider",
)


def build_snapshot(src: dict) -> dict:
    out: dict = {}
    for key, v in src.items():
        if not isinstance(v, dict):
            continue
        if v.get("input_cost_per_token") is None:
            continue
        mode = v.get("mode")
        if mode is not None and mode not in KEEP_MODES:
            continue
        out[key] = {f: v.get(f) for f in KEEP_FIELDS}
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Refresh the vendored LiteLLM pricing snapshot")
    ap.add_argument("--dry-run", action="store_true", help="show changes, write nothing")
    args = ap.parse_args()

    print(f"Fetching {UPSTREAM_URL} ...")
    req = urllib.request.Request(UPSTREAM_URL, headers={"User-Agent": "eval-mcp-pricing-sync"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        src = json.loads(resp.read().decode("utf-8"))

    new = build_snapshot(src)
    bedrock = sum(1 for v in new.values() if (v.get("litellm_provider") or "").startswith("bedrock"))
    print(f"Built snapshot: {len(new)} models ({bedrock} bedrock)")

    old = {}
    if SNAPSHOT.exists():
        old = json.loads(SNAPSHOT.read_text())

    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed = sorted(k for k in set(new) & set(old) if new[k] != old[k])
    print(f"  +{len(added)} added, -{len(removed)} removed, ~{len(changed)} changed")
    for k in added[:20]:
        print(f"    + {k}")
    for k in changed[:20]:
        print(f"    ~ {k}: {old[k].get('input_cost_per_token')} -> {new[k].get('input_cost_per_token')} in/tok")

    if args.dry_run:
        print("\n--dry-run: snapshot not written.")
        return 0

    SNAPSHOT.write_text(json.dumps(new, indent=0, sort_keys=True))
    print(f"\nWrote {SNAPSHOT} — review `git diff` before committing.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
