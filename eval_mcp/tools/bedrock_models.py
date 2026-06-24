"""
Canonical model discovery for Bedrock + external providers.

Single source of truth — the MCP tool wrappers in eval_mcp/server.py just
delegate here. Do not re-fork this logic into other modules.

No allowlist: we surface *every* text-capable model that AWS reports as
available in the account (inference profiles + foundation models). There is no
hand-maintained list to update — a newly launched model (e.g. a future Opus 5)
shows up automatically the moment AWS enables it. The historical concern (a
model that ships without Converse-API support) is handled where it belongs:
``run_eval.validate_providers`` runs a real Converse smoke test against each
chosen model before an eval starts and fails fast with an actionable message,
and pricing resolves live from LiteLLM (see core/pricing.py). So a broken or
unpriced model is caught at run time with a clear error rather than silently
hidden — and the maintenance burden is gone.
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


def list_bedrock_models(
    provider: str = "all",
    limit: int = 0,
    text_only: bool = True,
) -> dict:
    """
    Discover Bedrock models, dedup'd across inference profiles and foundation models.

    Returns a dict (callers JSON-encode). Surfaces every text-capable model AWS
    reports as available — no allowlist. Image/embedding models are filtered out
    in text_only mode; a model's actual eval-pipeline compatibility is verified
    by the Converse smoke test in run_eval.validate_providers at run time.
    """
    err = get_autodetect_error()
    if err is not None:
        return {"models": [], "count": 0, "error": str(err)}
    bedrock_client = create_boto3_bedrock_client('bedrock', region)

    # Patterns to exclude for text_only mode — non-text modalities and
    # task-specific models that aren't chat/generation endpoints.
    exclude_patterns = [
        'stability.', 'embed', 'upscale', 'inpaint', 'outpaint', 'image',
        'pegasus', 'rerank', 'sonic', 'vision', 'canvas', 'titan-tg',
    ]

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
        """Check if model should be included based on filters.

        No allowlist gate — only the text_only content filter and the optional
        provider filter. Run-time Converse validation is the compatibility gate.
        """
        # Skip non-text models if text_only
        if text_only and any(pat in model_id.lower() for pat in exclude_patterns):
            return False

        # Filter by provider if specified
        provider_name = extract_provider_name(model_id)
        if provider_filter.lower() != "all" and provider_filter.lower() != provider_name.lower():
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

            # Without an allowlist we must trust AWS's capability metadata to
            # avoid surfacing un-invokable base IDs. A foundation model is only
            # directly usable when it supports ON_DEMAND inference; models that
            # are INFERENCE_PROFILE-only (and lacked a profile above) or
            # PROVISIONED-only can't be called as-is, so skip them.
            inference_types = model.get('inferenceTypesSupported', [])
            if inference_types and 'ON_DEMAND' not in inference_types:
                continue
            # Text output only (mirrors text_only intent using AWS's modalities).
            output_modalities = model.get('outputModalities', [])
            if text_only and output_modalities and 'TEXT' not in output_modalities:
                continue
            # Skip retired models.
            if model.get('modelLifecycle', {}).get('status') == 'LEGACY':
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
        "note": (
            "Shows standard Bedrock (Converse) inference profiles and foundation "
            "models only. Use text_only=false to include image/embedding models. "
            "IMPORTANT: this does NOT include OpenAI GPT-5.x models, which run on "
            "the separate Bedrock Mantle endpoint — call list_available_models to "
            "see those (e.g. openai/bedrock/gpt-5.4). Never tell a user a model is "
            "unavailable based on this tool alone."
        ),
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
