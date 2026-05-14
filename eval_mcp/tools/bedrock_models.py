"""
Canonical model discovery for Bedrock + external providers.

Single source of truth — the MCP tool wrappers in eval_mcp/server.py just
delegate here. Do not re-fork this logic into other modules.

Why a curated SUPPORTED_MODELS allowlist: new Bedrock models occasionally
ship without Converse-API support or with payload quirks that break the
eval pipeline. Keeping the set explicit means a human acknowledges each
new model before agents start selecting it. Maintenance cost is real but
intentional — add new entries below when validating a new release.
"""

import os
from typing import Optional
from eval_mcp.core.bedrock_client import (
    create_boto3_bedrock_client,
    get_autodetect_error,
)
from eval_mcp.tools.external_providers import (
    detect_available_providers,
    get_external_models,
)

region = os.environ.get("AWS_REGION", "us-west-2")

# Curated models supported for evaluations. Updated Mar 2026.
# Base IDs for models invokable directly + us. prefixed IDs for models
# requiring cross-region inference profiles (newer Anthropic/Amazon/Meta).
SUPPORTED_MODELS = {
    # Amazon Nova
    "amazon.nova-micro-v1:0", "amazon.nova-lite-v1:0",
    "amazon.nova-pro-v1:0", "amazon.nova-premier-v1:0",
    "amazon.nova-2-lite-v1:0",
    # Anthropic Claude (base IDs listed for matching; require us. prefix to invoke)
    "anthropic.claude-opus-4-6-v1",
    "anthropic.claude-opus-4-5-20251101-v1:0", "anthropic.claude-opus-4-1-20250805-v1:0",
    "anthropic.claude-opus-4-20250514-v1:0",
    "anthropic.claude-sonnet-4-6",
    "anthropic.claude-sonnet-4-5-20250929-v1:0", "anthropic.claude-sonnet-4-20250514-v1:0",
    "anthropic.claude-haiku-4-5-20251001-v1:0",
    "anthropic.claude-3-7-sonnet-20250219-v1:0",
    "anthropic.claude-3-5-haiku-20241022-v1:0",
    "anthropic.claude-3-opus-20240229-v1:0", "anthropic.claude-3-haiku-20240307-v1:0",
    # Cohere
    "cohere.command-r-plus-v1:0", "cohere.command-r-v1:0",
    # DeepSeek
    "deepseek.r1-v1:0", "deepseek.v3-v1:0", "deepseek.v3.2",
    # Google Gemma
    "google.gemma-3-4b-it", "google.gemma-3-12b-it", "google.gemma-3-27b-it",
    # Meta Llama
    "meta.llama3-1-8b-instruct-v1:0", "meta.llama3-1-70b-instruct-v1:0",
    "meta.llama3-1-405b-instruct-v1:0", "meta.llama3-2-3b-instruct-v1:0",
    "meta.llama4-maverick-17b-instruct-v1:0", "meta.llama4-scout-17b-instruct-v1:0",
    # MiniMax
    "minimax.minimax-m2", "minimax.minimax-m2.1",
    # Mistral
    "mistral.mistral-7b-instruct-v0:2", "mistral.mixtral-8x7b-instruct-v0:1",
    "mistral.mistral-large-2402-v1:0", "mistral.mistral-large-2407-v1:0",
    "mistral.mistral-small-2402-v1:0",
    "mistral.mistral-large-3-675b-instruct", "mistral.devstral-2-123b",
    "mistral.magistral-small-2509",
    "mistral.ministral-3-3b-instruct", "mistral.ministral-3-8b-instruct", "mistral.ministral-3-14b-instruct",
    # Moonshot
    "moonshot.kimi-k2-thinking", "moonshotai.kimi-k2.5",
    # Nvidia Nemotron
    "nvidia.nemotron-nano-9b-v2", "nvidia.nemotron-nano-12b-v2", "nvidia.nemotron-nano-3-30b",
    # OpenAI GPT-OSS
    "openai.gpt-oss-120b-1:0", "openai.gpt-oss-20b-1:0",
    # Qwen
    "qwen.qwen3-235b-a22b-2507-v1:0", "qwen.qwen3-32b-v1:0",
    "qwen.qwen3-coder-30b-a3b-v1:0", "qwen.qwen3-coder-480b-a35b-v1:0",
    # Writer Palmyra
    "writer.palmyra-x4-v1:0", "writer.palmyra-x5-v1:0",
    # Z.AI GLM
    "zai.glm-4.7", "zai.glm-4.7-flash",
    # --- Cross-region inference profiles ---
    # us.
    "us.amazon.nova-2-lite-v1:0", "us.amazon.nova-lite-v1:0",
    "us.amazon.nova-micro-v1:0", "us.amazon.nova-premier-v1:0", "us.amazon.nova-pro-v1:0",
    "us.anthropic.claude-opus-4-6-v1",
    "us.anthropic.claude-opus-4-5-20251101-v1:0", "us.anthropic.claude-opus-4-1-20250805-v1:0",
    "us.anthropic.claude-opus-4-20250514-v1:0",
    "us.anthropic.claude-sonnet-4-6",
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0", "us.anthropic.claude-sonnet-4-20250514-v1:0",
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "us.anthropic.claude-3-7-sonnet-20250219-v1:0",
    "us.anthropic.claude-3-5-haiku-20241022-v1:0",
    "us.anthropic.claude-3-opus-20240229-v1:0", "us.anthropic.claude-3-haiku-20240307-v1:0",
    "us.deepseek.r1-v1:0",
    "us.meta.llama3-1-8b-instruct-v1:0", "us.meta.llama3-1-70b-instruct-v1:0",
    "us.meta.llama3-1-405b-instruct-v1:0",
    "us.meta.llama3-2-1b-instruct-v1:0", "us.meta.llama3-2-3b-instruct-v1:0",
    "us.meta.llama3-2-11b-instruct-v1:0", "us.meta.llama3-2-90b-instruct-v1:0",
    "us.meta.llama3-3-70b-instruct-v1:0",
    "us.meta.llama4-maverick-17b-instruct-v1:0", "us.meta.llama4-scout-17b-instruct-v1:0",
    # eu.
    "eu.amazon.nova-2-lite-v1:0", "eu.amazon.nova-lite-v1:0",
    "eu.amazon.nova-micro-v1:0", "eu.amazon.nova-premier-v1:0", "eu.amazon.nova-pro-v1:0",
    "eu.anthropic.claude-3-7-sonnet-20250219-v1:0",
    "eu.anthropic.claude-3-haiku-20240307-v1:0", "eu.anthropic.claude-haiku-4-5-20251001-v1:0",
    "eu.anthropic.claude-opus-4-1-20250805-v1:0", "eu.anthropic.claude-opus-4-5-20251101-v1:0",
    "eu.anthropic.claude-sonnet-4-20250514-v1:0", "eu.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "eu.meta.llama3-2-1b-instruct-v1:0", "eu.meta.llama3-2-3b-instruct-v1:0",
    "eu.meta.llama4-maverick-17b-instruct-v1:0", "eu.meta.llama4-scout-17b-instruct-v1:0",
    # apac.
    "apac.amazon.nova-2-lite-v1:0", "apac.amazon.nova-lite-v1:0",
    "apac.amazon.nova-micro-v1:0", "apac.amazon.nova-premier-v1:0", "apac.amazon.nova-pro-v1:0",
    "apac.anthropic.claude-3-haiku-20240307-v1:0",
    "apac.anthropic.claude-haiku-4-5-20251001-v1:0", "apac.anthropic.claude-opus-4-1-20250805-v1:0",
    "apac.anthropic.claude-opus-4-5-20251101-v1:0", "apac.anthropic.claude-sonnet-4-20250514-v1:0",
    "apac.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "apac.meta.llama4-maverick-17b-instruct-v1:0", "apac.meta.llama4-scout-17b-instruct-v1:0",
    # global.
    "global.amazon.nova-2-lite-v1:0",
    # us-gov.
    "us-gov.anthropic.claude-3-haiku-20240307-v1:0",
}


def list_bedrock_models(
    provider: str = "all",
    limit: int = 0,
    text_only: bool = True,
) -> dict:
    """
    Discover Bedrock models, dedup'd across inference profiles and foundation models.

    Returns a dict (callers JSON-encode). Filtered against SUPPORTED_MODELS
    so only entries a human has validated for the eval pipeline are surfaced.
    """
    err = get_autodetect_error()
    if err is not None:
        return {"models": [], "count": 0, "error": str(err)}
    bedrock_client = create_boto3_bedrock_client('bedrock', region)

    # Patterns to exclude for text_only mode
    exclude_patterns = ['stability.', 'embed', 'upscale', 'inpaint', 'outpaint', 'image', 'pegasus']

    # Track base model IDs (without regional prefix) to avoid duplicates
    # e.g., if we see us.anthropic.claude-*, we mark anthropic.claude-* as seen
    # so foundation model anthropic.claude-* won't be added (it requires inference profile)
    seen_base_ids = set()
    models = []

    def strip_regional_prefix(model_id: str) -> str:
        """Strip regional prefix (e.g., 'us.anthropic.claude-...' -> 'anthropic.claude-...')."""
        parts = model_id.split('.', 1)
        if len(parts) >= 2 and parts[0] in ('us', 'eu', 'apac', 'global', 'us-gov'):
            return parts[1]
        return model_id

    def extract_provider_name(model_id: str) -> str:
        """Extract provider from model ID (e.g., 'us.anthropic.claude-...' -> 'anthropic')."""
        parts = model_id.split('.')
        if len(parts) >= 2:
            # Skip regional prefix if present (us., eu., apac., global.)
            if parts[0] in ('us', 'eu', 'apac', 'global', 'us-gov'):
                return parts[1]
            return parts[0]
        return "unknown"

    def should_include(model_id: str, model_name: str, provider_filter: str) -> bool:
        """Check if model should be included based on filters."""
        # Skip non-text models if text_only
        if text_only and any(pat in model_id.lower() for pat in exclude_patterns):
            return False

        # Filter by provider if specified
        provider_name = extract_provider_name(model_id)
        if provider_filter.lower() != "all" and provider_filter.lower() != provider_name.lower():
            return False

        # Only include supported models
        if model_id not in SUPPORTED_MODELS:
            return False

        return True

    # 1. Query inference profiles (cross-region endpoints) — PAGINATED.
    # Earlier versions fetched a single page, which silently dropped newer
    # models (claude-sonnet-4-6, opus-4-7, ...) once AWS shipped enough
    # profiles to overflow the first response.
    try:
        paginator = bedrock_client.get_paginator('list_inference_profiles')
        for page in paginator.paginate(typeEquals='SYSTEM_DEFINED'):
            for profile in page.get('inferenceProfileSummaries', []):
                profile_id = profile.get('inferenceProfileId', '')
                profile_name = profile.get('inferenceProfileName', '')

                if not should_include(profile_id, profile_name, provider):
                    continue

                # Mark base ID as seen so foundation model won't duplicate
                seen_base_ids.add(strip_regional_prefix(profile_id))
                models.append({
                    'id': f"bedrock/{profile_id}",
                    'modelId': profile_id,
                    'name': profile_name,
                    'provider': extract_provider_name(profile_id),
                    'type': 'inference_profile',
                })
    except Exception:
        pass  # Continue to foundation models even if profiles fail

    # 2. Query foundation models (includes models without inference profiles like Nemotron)
    try:
        foundation_response = bedrock_client.list_foundation_models()
        for model in foundation_response.get('modelSummaries', []):
            model_id = model.get('modelId', '')
            model_name = model.get('modelName', '')

            # Skip if this model has an inference profile (use that instead)
            base_id = strip_regional_prefix(model_id)
            if base_id in seen_base_ids:
                continue

            if not should_include(model_id, model_name, provider):
                continue

            seen_base_ids.add(base_id)
            models.append({
                'id': f"bedrock/{model_id}",
                'modelId': model_id,
                'name': model_name,
                'provider': extract_provider_name(model_id),
                'type': 'foundation_model',
            })
    except Exception as e:
        pass  # Return whatever we have

    if not models:
        return {
            "models": [],
            "count": 0,
            "error": "Failed to list models",
            "note": "Check AWS credentials and bedrock:ListInferenceProfiles / bedrock:ListFoundationModels permissions.",
        }

    models.sort(key=lambda x: (x['provider'], x['name']))

    truncated = False
    if limit > 0 and limit < len(models):
        models = models[:limit]
        truncated = True

    return {
        "models": models,
        "count": len(models),
        "truncated": truncated,
        "filters": {
            "provider": provider,
            "limit": limit,
            "text_only": text_only,
        },
        "note": "Shows inference profiles and foundation models. Use text_only=false to include image/embedding models.",
    }


def list_available_models(
    provider: str = "all",
    source: str = "all",
) -> dict:
    """
    Combine Bedrock + external-provider models into one list.

    Returns a dict (callers JSON-encode). External providers appear only when
    their API key env var is set (see external_providers.EXTERNAL_PROVIDERS).
    """
    all_models = []
    bedrock_error: Optional[str] = None

    if source in ("all", "bedrock"):
        try:
            bedrock_result = list_bedrock_models(provider=provider)
            for m in bedrock_result.get("models", []):
                m["source"] = "bedrock"
                all_models.append(m)
            # list_bedrock_models surfaces autodetect / credential issues in
            # an "error" field rather than raising; propagate it so the user
            # sees the actionable message even when external providers also
            # come back empty.
            if bedrock_result.get("error"):
                bedrock_error = bedrock_result["error"]
        except Exception:
            pass

    if source in ("all", "external"):
        external = get_external_models(provider=provider)
        for m in external:
            m["source"] = "external"
            m["type"] = "external"
        all_models.extend(external)

    available_providers = detect_available_providers()

    if not all_models:
        out = {
            "models": [],
            "count": 0,
            "available_providers": available_providers,
            "note": "No models found. Check AWS credentials for Bedrock, or configure API keys for external providers (make keys / deploy.sh --keys).",
        }
        if bedrock_error:
            out["error"] = bedrock_error
        return out

    return {
        "models": all_models,
        "count": len(all_models),
        "available_providers": available_providers,
        "note": "Models from Bedrock and external providers. Use source='bedrock' or source='external' to filter.",
    }
