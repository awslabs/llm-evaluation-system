"""
External (non-Bedrock) LLM provider definitions.

Detects which providers are available based on environment variables
and returns curated model lists for each.

To add a new provider:
  1. Add an entry to EXTERNAL_PROVIDERS below
  2. Add the env var name to scripts/parse-env-keys.sh ALLOWED_KEY_NAMES
"""

import os
from typing import Any

# Registry of external providers. Each entry maps a provider name to:
#   env_var:      environment variable that must be set to enable this provider
#   display_name: human-readable name for UI/agent display
#   models:       curated list of models — IDs use Inspect AI's provider/model format
EXTERNAL_PROVIDERS: dict[str, dict[str, Any]] = {
    "openai": {
        "env_var": "OPENAI_API_KEY",
        "display_name": "OpenAI",
        "models": [
            # GPT-5.4 series (latest)
            {"id": "openai/gpt-5.4", "name": "GPT-5.4"},
            {"id": "openai/gpt-5.4-mini", "name": "GPT-5.4 Mini"},
            {"id": "openai/gpt-5.4-nano", "name": "GPT-5.4 Nano"},
            {"id": "openai/gpt-5.4-pro", "name": "GPT-5.4 Pro"},
            # GPT-5.2 series
            {"id": "openai/gpt-5.2", "name": "GPT-5.2"},
            {"id": "openai/gpt-5.2-pro", "name": "GPT-5.2 Pro"},
            # GPT-5.1 series
            {"id": "openai/gpt-5.1", "name": "GPT-5.1"},
            # GPT-5 series
            {"id": "openai/gpt-5", "name": "GPT-5"},
            {"id": "openai/gpt-5-mini", "name": "GPT-5 Mini"},
            {"id": "openai/gpt-5-nano", "name": "GPT-5 Nano"},
            {"id": "openai/gpt-5-pro", "name": "GPT-5 Pro"},
            # GPT-4.1 series
            {"id": "openai/gpt-4.1", "name": "GPT-4.1"},
            {"id": "openai/gpt-4.1-mini", "name": "GPT-4.1 Mini"},
            {"id": "openai/gpt-4.1-nano", "name": "GPT-4.1 Nano"},
            # GPT-4o series
            {"id": "openai/gpt-4o", "name": "GPT-4o"},
            {"id": "openai/gpt-4o-mini", "name": "GPT-4o Mini"},
            # o-series reasoning
            {"id": "openai/o4-mini", "name": "o4 Mini"},
            {"id": "openai/o3", "name": "o3"},
            {"id": "openai/o3-pro", "name": "o3 Pro"},
            {"id": "openai/o3-mini", "name": "o3 Mini"},
            {"id": "openai/o1", "name": "o1"},
            {"id": "openai/o1-pro", "name": "o1 Pro"},
            {"id": "openai/o1-mini", "name": "o1 Mini"},
        ],
    },
    "anthropic": {
        "env_var": "ANTHROPIC_API_KEY",
        "display_name": "Anthropic (Direct API)",
        "models": [
            # Claude 4.6
            {"id": "anthropic/claude-opus-4-6", "name": "Claude Opus 4.6"},
            {"id": "anthropic/claude-sonnet-4-6", "name": "Claude Sonnet 4.6"},
            # Claude 4.5
            {"id": "anthropic/claude-opus-4-5-20251101", "name": "Claude Opus 4.5"},
            {"id": "anthropic/claude-sonnet-4-5-20250929", "name": "Claude Sonnet 4.5"},
            {"id": "anthropic/claude-haiku-4-5-20251001", "name": "Claude Haiku 4.5"},
            # Claude 4.1
            {"id": "anthropic/claude-opus-4-1-20250805", "name": "Claude Opus 4.1"},
            # Claude 3.x
            {"id": "anthropic/claude-3-7-sonnet-20250219", "name": "Claude 3.7 Sonnet"},
            {"id": "anthropic/claude-3-5-sonnet-20241022", "name": "Claude 3.5 Sonnet"},
            {"id": "anthropic/claude-3-5-haiku-20241022", "name": "Claude 3.5 Haiku"},
            {"id": "anthropic/claude-3-opus-20240229", "name": "Claude 3 Opus"},
            {"id": "anthropic/claude-3-haiku-20240307", "name": "Claude 3 Haiku"},
        ],
    },
    "google": {
        "env_var": "GOOGLE_API_KEY",
        "display_name": "Google (Gemini)",
        "models": [
            # Gemini 3.x
            {"id": "google/gemini-3.1-pro-preview", "name": "Gemini 3.1 Pro"},
            {"id": "google/gemini-3.1-flash-lite-preview", "name": "Gemini 3.1 Flash Lite"},
            {"id": "google/gemini-3-flash-preview", "name": "Gemini 3 Flash"},
            # Gemini 2.5
            {"id": "google/gemini-2.5-pro", "name": "Gemini 2.5 Pro"},
            {"id": "google/gemini-2.5-flash", "name": "Gemini 2.5 Flash"},
            {"id": "google/gemini-2.5-flash-lite", "name": "Gemini 2.5 Flash Lite"},
            # Gemini 2.0
            {"id": "google/gemini-2.0-flash", "name": "Gemini 2.0 Flash"},
            {"id": "google/gemini-2.0-flash-lite", "name": "Gemini 2.0 Flash Lite"},
            # Gemini 1.5
            {"id": "google/gemini-1.5-pro", "name": "Gemini 1.5 Pro"},
            {"id": "google/gemini-1.5-flash", "name": "Gemini 1.5 Flash"},
        ],
    },
}


def detect_available_providers() -> list[dict[str, Any]]:
    """
    Detect which external providers are available based on environment variables.

    Returns a list of dicts with: name, display_name, model_count
    """
    available = []
    for name, config in EXTERNAL_PROVIDERS.items():
        key = os.environ.get(config["env_var"], "")
        if key:  # non-empty string
            available.append({
                "name": name,
                "display_name": config["display_name"],
                "model_count": len(config["models"]),
            })
    return available


def get_external_models(provider: str = "all") -> list[dict[str, Any]]:
    """
    Get models for available external providers.

    Args:
        provider: Filter by provider name, or "all" for all available.

    Returns:
        List of model dicts with: id, name, provider
    """
    models = []
    for name, config in EXTERNAL_PROVIDERS.items():
        # Skip if provider filter doesn't match
        if provider != "all" and provider != name:
            continue

        # Skip if API key not set
        key = os.environ.get(config["env_var"], "")
        if not key:
            continue

        for model in config["models"]:
            models.append({
                "id": model["id"],
                "name": model["name"],
                "provider": name,
            })

    return models
