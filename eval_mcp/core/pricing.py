"""Model pricing calculation from provider_pricing.json.

Prices are in $/million tokens. Cost = (input_tokens * input_price + output_tokens * output_price) / 1_000_000
"""

import json
from pathlib import Path

_PRICING_FILE = Path(__file__).parent / "provider_pricing.json"
_pricing_data: dict | None = None


def _load_pricing() -> dict:
    global _pricing_data
    if _pricing_data is None:
        _pricing_data = json.loads(_PRICING_FILE.read_text())
    return _pricing_data


def _normalize_model_id(model_id: str) -> tuple[str, str]:
    """Extract provider section and base model name from a model ID.

    Accepts both Inspect AI style ("bedrock/...") and OTel GenAI semconv style
    ("aws.bedrock/..."). Keeps other providers (openai, anthropic, google, ...)
    as-is since those names already match between Inspect and OTel.

    Examples:
        "bedrock/us.anthropic.claude-sonnet-4-6"     -> ("bedrock", "anthropic.claude-sonnet-4-6")
        "aws.bedrock/us.anthropic.claude-sonnet-4-6" -> ("bedrock", "anthropic.claude-sonnet-4-6")
        "openai/gpt-4o"                              -> ("openai", "gpt-4o")
        "anthropic/claude-opus-4-6"                  -> ("anthropic", "claude-opus-4-6")
        "google/gemini-2.5-pro"                      -> ("google", "gemini-2.5-pro")
    """
    # Split provider prefix
    if "/" in model_id:
        provider, model = model_id.split("/", 1)
    else:
        return "", model_id

    # Map OTel's gen_ai.system values to the names used in our pricing table.
    # See https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-spans/
    _OTEL_PROVIDER_ALIASES = {"aws.bedrock": "bedrock"}
    provider = _OTEL_PROVIDER_ALIASES.get(provider, provider)

    # For bedrock, strip region prefix (us., eu., apac., etc.) and version suffix (-v1:0)
    if provider == "bedrock":
        # Strip region prefix
        for prefix in ("us.", "eu.", "apac.", "global.", "us-gov."):
            if model.startswith(prefix):
                model = model[len(prefix):]
                break
        # Strip version suffix like -v1:0, -v1, -20251101-v1:0
        import re
        model = re.sub(r"-v\d+:\d+$", "", model)
        model = re.sub(r"-v\d+$", "", model)
        model = re.sub(r"-\d{8}-v\d+:\d+$", "", model)
        model = re.sub(r"-\d{8}-v\d+$", "", model)
        # Strip instance suffix like -instruct, keeping the base
        # e.g. "meta.llama3-1-70b-instruct" -> try with and without
        return "bedrock", model

    return provider, model


def calculate_cost(model_id: str, input_tokens: int, output_tokens: int) -> float | None:
    """Calculate cost in dollars for a model call.

    Returns None if model not found in pricing table.
    """
    pricing = _load_pricing()
    provider, model = _normalize_model_id(model_id)

    # Look up in the appropriate section
    section = pricing.get(provider, {})
    if model in section:
        p = section[model]
        return (input_tokens * p["input"] + output_tokens * p["output"]) / 1_000_000

    # Try progressively shorter model names (strip suffixes like -instruct)
    parts = model.rsplit("-", 1)
    while len(parts) == 2:
        shorter = parts[0]
        if shorter in section:
            p = section[shorter]
            return (input_tokens * p["input"] + output_tokens * p["output"]) / 1_000_000
        parts = shorter.rsplit("-", 1)

    return None
