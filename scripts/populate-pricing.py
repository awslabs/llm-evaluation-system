#!/usr/bin/env python3
"""
Populate provider_pricing.json with verified prices from pydantic/genai-prices.

Bedrock section is kept from the existing file (authoritative — from AWS Pricing API).
OpenAI, Anthropic, and Google sections are fetched from pydantic/genai-prices.

Usage:
    uv run --with genai-prices scripts/populate-pricing.py
    uv run --with genai-prices scripts/populate-pricing.py --dry-run
"""

import argparse
import json
import sys
from pathlib import Path

try:
    import genai_prices
    from genai_prices import types
except ImportError:
    print("ERROR: genai-prices not installed.")
    print("Run: uv run --with genai-prices scripts/populate-pricing.py")
    sys.exit(1)

SCRIPT_DIR = Path(__file__).parent
PROJECT_DIR = SCRIPT_DIR.parent
PRICING_FILE = PROJECT_DIR / "eval_mcp" / "core" / "provider_pricing.json"

# Models to include per provider. Add new models here as they're released.
OPENAI_MODELS = [
    "gpt-4o",
    "gpt-4o-mini",
    "gpt-4.1",
    "gpt-4.1-mini",
    "gpt-4.1-nano",
    "o3",
    "o3-mini",
    "o4-mini",
    "o1",
    "o1-mini",
]

ANTHROPIC_MODELS = [
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "claude-opus-4-5",
    "claude-sonnet-4-5",
    "claude-haiku-4-5",
    "claude-3-7-sonnet",
    "claude-3-5-sonnet",
    "claude-3-5-haiku",
    "claude-3-opus",
    "claude-3-haiku",
]

GOOGLE_MODELS = [
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
    "gemini-1.5-pro",
    "gemini-1.5-flash",
]


def get_price(model_id: str, provider_id: str) -> dict | None:
    """Fetch price from genai-prices. Returns {input, output} in $/MTok or None."""
    try:
        usage = types.Usage(input_tokens=1, output_tokens=1)
        result = genai_prices.calc_price(usage, model_id, provider_id=provider_id)
        mp = result.model_price

        def extract_mtok(val):
            if val is None:
                return None
            if hasattr(val, 'base'):
                return float(val.base)
            try:
                return float(val)
            except (TypeError, ValueError):
                return None

        inp = extract_mtok(mp.input_mtok)
        out = extract_mtok(mp.output_mtok)
        if inp is None or out is None:
            return None
        return {"input": round(inp, 6), "output": round(out, 6)}
    except Exception:
        return None


def build_section(models: list[str], provider_id: str) -> tuple[dict, list[str]]:
    """Build a pricing section. Returns (prices_dict, missing_models)."""
    section = {}
    missing = []
    for model in models:
        price = get_price(model, provider_id)
        if price:
            section[model] = price
        else:
            missing.append(model)
    return section, missing


def main():
    parser = argparse.ArgumentParser(description="Populate provider_pricing.json from pydantic/genai-prices")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without writing")
    args = parser.parse_args()

    # Load existing pricing to preserve bedrock section
    if PRICING_FILE.exists():
        with open(PRICING_FILE) as f:
            existing = json.load(f)
    else:
        existing = {}

    bedrock = existing.get("bedrock", {})

    print("Fetching OpenAI pricing...")
    openai, openai_missing = build_section(OPENAI_MODELS, "openai")

    print("Fetching Anthropic pricing...")
    anthropic, anthropic_missing = build_section(ANTHROPIC_MODELS, "anthropic")

    print("Fetching Google pricing...")
    google, google_missing = build_section(GOOGLE_MODELS, "google")

    new_pricing = {
        "bedrock": bedrock,
        "openai": openai,
        "anthropic": anthropic,
        "google": google,
    }

    # Show summary
    print()
    print("=== Summary ===")
    print(f"  bedrock:   {len(bedrock)} models (unchanged — from AWS Pricing API)")
    print(f"  openai:    {len(openai)} models fetched", end="")
    print(f"  [{', '.join(openai_missing)}]" if openai_missing else "")
    print(f"  anthropic: {len(anthropic)} models fetched", end="")
    print(f"  [{', '.join(anthropic_missing)}]" if anthropic_missing else "")
    print(f"  google:    {len(google)} models fetched", end="")
    print(f"  [{', '.join(google_missing)}]" if google_missing else "")

    if openai_missing or anthropic_missing or google_missing:
        print()
        print("WARNING: Some models not found in genai-prices — they will be missing from the output.")

    if args.dry_run:
        print()
        print("Dry run — no changes written.")
        print("Output would be:")
        print(json.dumps(new_pricing, indent=2)[:500] + "...")
        return

    with open(PRICING_FILE, "w") as f:
        json.dump(new_pricing, f, indent=2)
        f.write("\n")

    print()
    print(f"Written to {PRICING_FILE}")
    print("Run tests: python -m pytest tests/test_provider_pricing.py -v")


if __name__ == "__main__":
    main()
