"""
External (non-Bedrock) LLM provider definitions.

Detects which providers are available based on environment variables
and returns curated model lists for each.

To add a new provider:
  1. Add an entry to EXTERNAL_PROVIDERS below
  2. Add the env var name to scripts/parse-env-keys.sh ALLOWED_KEY_NAMES
"""

import os
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# .env.keys live-reload
#
# The MCP server is long-lived; users may add provider keys to .env.keys
# after the process starts. We re-read the file on each external-provider
# lookup (mtime-gated) so keys become usable without a server restart.
# ---------------------------------------------------------------------------

_KEYS_CACHE: dict[str, Any] = {"path": None, "mtime": None}


def _candidate_keys_files() -> list[Path]:
    paths: list[Path] = []
    override = os.environ.get("EVAL_MCP_KEYS_FILE")
    if override:
        paths.append(Path(override))
    paths.append(Path.cwd() / ".env.keys")
    paths.append(Path.home() / ".eval-mcp" / ".env.keys")
    return paths


def _parse_env_file(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        for raw_line in path.read_text().splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                result[key] = value
    except OSError:
        return {}
    return result


def _aws_credentials_available() -> bool:
    """True if the ambient AWS credential chain can be resolved.

    Used to gate AWS-credential-based providers (Bedrock Mantle) that have no
    API key. Cheap and offline — botocore just walks the credential providers
    (env, profile, SSO, instance/role) without making a network call.
    """
    try:
        import boto3

        return boto3.Session().get_credentials() is not None
    except Exception:
        return False


def _refresh_keys_from_file() -> None:
    """Merge .env.keys entries into os.environ if the file has changed."""
    allowed = {cfg["env_var"] for cfg in EXTERNAL_PROVIDERS.values() if cfg["env_var"]}
    for path in _candidate_keys_files():
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if _KEYS_CACHE["path"] == str(path) and _KEYS_CACHE["mtime"] == mtime:
            return
        for key, value in _parse_env_file(path).items():
            if key in allowed and value:
                os.environ[key] = value
        _KEYS_CACHE["path"] = str(path)
        _KEYS_CACHE["mtime"] = mtime
        return

# Registry of external providers. Each entry maps a provider name to:
#   env_var:      environment variable that must be set to enable this provider.
#                 None means the provider is gated by ambient AWS credentials
#                 instead of an API key (see "bedrock-mantle" below).
#   display_name: human-readable name for UI/agent display
#   models:       curated list of models — IDs use Inspect AI's provider/model format
EXTERNAL_PROVIDERS: dict[str, dict[str, Any]] = {
    # OpenAI frontier models hosted on Amazon Bedrock via the Mantle endpoint
    # (bedrock-mantle, OpenAI-compatible Responses API). These are NOT on
    # bedrock-runtime/Converse, so they never appear in list_foundation_models —
    # they must be surfaced explicitly. Inspect resolves the `openai/bedrock/<id>`
    # string natively: it derives the bedrock-mantle URL, mints a short-lived
    # bearer token from the ambient AWS credentials, and routes through the
    # Responses API. Auth is AWS creds (the same chain the rest of the app uses),
    # so there's no API key — env_var is None and availability follows from
    # having AWS credentials. Whether the account is actually entitled (C-score /
    # model access) is proven by the run-time validation in run_eval, matching
    # our "no allowlist; validate at run time" approach for Bedrock models.
    "bedrock-mantle": {
        "env_var": None,
        "display_name": "OpenAI on Bedrock (Mantle)",
        # match_aliases: a provider filter for any of these names ALSO returns
        # this provider's models. GPT-5.4/5.5 are OpenAI models, so a caller (or
        # the agent) filtering provider="openai" must find them here even though
        # the canonical provider key is "bedrock-mantle". Without this, asking
        # "is GPT-5.4 available?" filters to provider=openai and wrongly misses
        # the Mantle models.
        "match_aliases": ["openai"],
        "models": [
            {"id": "openai/bedrock/gpt-5.5", "name": "GPT-5.5 (Bedrock)"},
            {"id": "openai/bedrock/gpt-5.4", "name": "GPT-5.4 (Bedrock)"},
        ],
    },
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
    _refresh_keys_from_file()
    available = []
    for name, config in EXTERNAL_PROVIDERS.items():
        if _provider_enabled(config):
            available.append({
                "name": name,
                "display_name": config["display_name"],
                "model_count": len(config["models"]),
            })
    return available


def _provider_enabled(config: dict[str, Any]) -> bool:
    """Whether a provider is usable: API key set, or AWS creds for key-less ones."""
    env_var = config["env_var"]
    if env_var:
        return bool(os.environ.get(env_var, ""))
    # Key-less provider (Bedrock Mantle) — gated by ambient AWS credentials.
    return _aws_credentials_available()


def get_external_models(provider: str = "all") -> list[dict[str, Any]]:
    """
    Get models for available external providers.

    Args:
        provider: Filter by provider name, or "all" for all available.

    Returns:
        List of model dicts with: id, name, provider
    """
    _refresh_keys_from_file()
    models = []
    for name, config in EXTERNAL_PROVIDERS.items():
        # Skip if provider filter doesn't match the canonical name or any alias
        # (e.g. provider="openai" also matches the "bedrock-mantle" provider,
        # whose models are OpenAI models hosted on Bedrock).
        if provider != "all" and provider != name and provider not in config.get("match_aliases", []):
            continue

        # Skip if the provider isn't usable (no API key, or no AWS creds for
        # the key-less Bedrock Mantle provider)
        if not _provider_enabled(config):
            continue

        for model in config["models"]:
            models.append({
                "id": model["id"],
                "name": model["name"],
                "provider": name,
            })

    return models
