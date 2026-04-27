#!/usr/bin/env python3
"""
Providers MCP Server - HTTP implementation.

Provides model discovery across Bedrock and external providers (OpenAI, Anthropic, Google, etc.).
External providers are detected based on API key environment variables.
"""

import json
import os
from mcp.server import FastMCP
from backend.core.bedrock_client import create_boto3_bedrock_client
from backend.mcp_servers.providers.external_providers import (
    detect_available_providers,
    get_external_models,
)

# Get configuration
region = os.environ.get("AWS_REGION", "us-west-2")
port = int(os.environ.get("PROVIDERS_MCP_SERVER_PORT", "8004"))
host = os.environ.get("HOST", "127.0.0.1")

# Initialize FastMCP server
mcp = FastMCP("providers-server", port=port, host=host)

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


@mcp.tool()
def list_bedrock_models(
    provider: str = "all",
    limit: int = 0,
    text_only: bool = True
) -> str:
    """
    Get list of AWS Bedrock models available for evaluations.

    Queries both inference profiles (cross-region) and foundation models to return
    all models you have access to. Returns models with correct format (bedrock/*) ready to use.

    Args:
        provider: Filter by provider name (case-insensitive):
            - "all" (default): All providers
            - "anthropic": Anthropic Claude models
            - "meta": Meta Llama models
            - "mistral": Mistral AI models
            - "amazon": Amazon Nova models
            - "deepseek": DeepSeek models
            - "nvidia": NVIDIA Nemotron models
            - Or any provider name

        limit: Maximum number of models to return (default: 0 = unlimited)

        text_only: If True (default), exclude image/embedding models

    Returns:
        JSON with available model IDs in bedrock:* format, sorted by provider
    """
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

    # 1. Query inference profiles (cross-region endpoints)
    try:
        profiles_response = bedrock_client.list_inference_profiles()
        for profile in profiles_response.get('inferenceProfileSummaries', []):
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
    except Exception as e:
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
        return json.dumps({
            "error": "Failed to list models",
            "note": "Check AWS credentials and permissions"
        })

    # Sort by provider, then by name
    models.sort(key=lambda x: (x['provider'], x['name']))

    # Apply limit
    if limit > 0 and limit < len(models):
        models = models[:limit]
        truncated = True
    else:
        truncated = False

    return json.dumps({
        "models": models,
        "count": len(models),
        "truncated": truncated,
        "filters": {
            "provider": provider,
            "limit": limit,
            "text_only": text_only
        },
        "note": "Shows inference profiles and foundation models. Use text_only=false to include image/embedding models."
    }, indent=2)


@mcp.tool()
def list_available_models(
    provider: str = "all",
    source: str = "all",
) -> str:
    """
    List all models available for evaluations, across Bedrock and external providers.

    Combines AWS Bedrock models with any external providers that have API keys configured
    (OpenAI, Anthropic direct, Google Gemini, etc.).

    Args:
        provider: Filter by provider name (case-insensitive):
            - "all" (default): All providers
            - "openai": OpenAI models (requires OPENAI_API_KEY)
            - "anthropic": Anthropic models (Bedrock + direct API if key set)
            - "google": Google Gemini models (requires GOOGLE_API_KEY)
            - Or any Bedrock provider name (amazon, meta, mistral, etc.)

        source: Filter by source:
            - "all" (default): Bedrock + external providers
            - "bedrock": Only AWS Bedrock models
            - "external": Only external provider models (OpenAI, Anthropic direct, Google, etc.)

    Returns:
        JSON with available models from all configured providers, sorted by source then provider
    """
    all_models = []

    # 1. Get Bedrock models (unless filtered to external only)
    if source in ("all", "bedrock"):
        try:
            bedrock_result = json.loads(list_bedrock_models(provider=provider))
            if "models" in bedrock_result:
                for m in bedrock_result["models"]:
                    m["source"] = "bedrock"
                all_models.extend(bedrock_result["models"])
        except Exception:
            pass  # Bedrock may not be available

    # 2. Get external models (unless filtered to bedrock only)
    if source in ("all", "external"):
        external = get_external_models(provider=provider)
        for m in external:
            m["source"] = "external"
            m["type"] = "external"
        all_models.extend(external)

    # Detect which external providers are configured
    available_providers = detect_available_providers()

    if not all_models:
        return json.dumps({
            "models": [],
            "count": 0,
            "available_providers": available_providers,
            "note": "No models found. Check AWS credentials for Bedrock, or configure API keys for external providers (make keys / deploy.sh --keys)."
        })

    return json.dumps({
        "models": all_models,
        "count": len(all_models),
        "available_providers": available_providers,
        "note": "Models from Bedrock and external providers. Use source='bedrock' or source='external' to filter."
    }, indent=2)


if __name__ == "__main__":
    print(f"✓ Starting Providers MCP Server on http://{host}:{port}/mcp")
    mcp.run(transport="streamable-http")
