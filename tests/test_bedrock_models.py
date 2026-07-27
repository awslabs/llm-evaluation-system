"""Regression tests for bedrock model discovery.

Specifically guards the pagination fix: if anyone replaces
`get_paginator("list_inference_profiles").paginate(...)` with a single
call, models on page 2+ silently disappear from the listing. This is
exactly the bug that made `claude-sonnet-4-6` invisible to the agent.
"""

from unittest.mock import MagicMock, patch

from eval_mcp.tools import bedrock_models


def _fake_client(pages, foundation_models=None):
    """Build a mock bedrock client whose paginator yields `pages`.

    Each page is a list of (profile_id, profile_name) tuples.
    """
    client = MagicMock()

    def paginate(**_kwargs):
        for page in pages:
            yield {
                "inferenceProfileSummaries": [
                    {"inferenceProfileId": pid, "inferenceProfileName": name}
                    for pid, name in page
                ]
            }

    paginator = MagicMock()
    paginator.paginate.side_effect = paginate
    client.get_paginator.return_value = paginator

    client.list_foundation_models.return_value = {
        "modelSummaries": [
            {
                "modelId": mid,
                "modelName": name,
                # Default to a directly-invokable text model; tests that care
                # about capability gating pass explicit dicts instead of tuples.
                "inferenceTypesSupported": ["ON_DEMAND"],
                "outputModalities": ["TEXT"],
            }
            if isinstance(name, str)
            else {"modelId": mid, "modelName": mid, **name}
            for mid, name in (foundation_models or [])
        ]
    }
    return client


def test_inference_profiles_are_paginated():
    """A profile on page 2 must be returned — this is the core bug fix."""
    pages = [
        [("us.anthropic.claude-3-haiku-20240307-v1:0", "US Claude 3 Haiku")],
        [("us.anthropic.claude-sonnet-4-6", "US Claude Sonnet 4.6")],  # page 2
    ]
    with patch.object(bedrock_models, "create_boto3_bedrock_client", return_value=_fake_client(pages)):
        result = bedrock_models.list_bedrock_models(provider="anthropic")

    ids = [m["modelId"] for m in result["models"]]
    assert "us.anthropic.claude-sonnet-4-6" in ids, (
        "claude-sonnet-4-6 was on page 2 of list_inference_profiles and "
        "got dropped — pagination regressed."
    )
    assert "us.anthropic.claude-3-haiku-20240307-v1:0" in ids


def test_no_allowlist_unknown_models_surface():
    """There is no allowlist: any text-capable model AWS reports must appear,
    including a brand-new one we've never seen (e.g. a future Opus 5). The
    Converse smoke test in run_eval is the compatibility gate, not discovery."""
    pages = [[("us.anthropic.claude-opus-5-v1:0", "US Claude Opus 5")]]
    with patch.object(bedrock_models, "create_boto3_bedrock_client", return_value=_fake_client(pages)):
        result = bedrock_models.list_bedrock_models(provider="anthropic")
    ids = [m["modelId"] for m in result["models"]]
    assert "us.anthropic.claude-opus-5-v1:0" in ids


def test_foundation_model_requires_on_demand():
    """A foundation model that is NOT directly invokable (INFERENCE_PROFILE-only,
    with no profile listed) must be skipped to avoid surfacing un-callable IDs."""
    foundation = [
        ("anthropic.future-profile-only-v1:0", {"inferenceTypesSupported": ["INFERENCE_PROFILE"]}),
        ("amazon.callable-v1:0", {"inferenceTypesSupported": ["ON_DEMAND"], "outputModalities": ["TEXT"]}),
    ]
    with patch.object(
        bedrock_models,
        "create_boto3_bedrock_client",
        return_value=_fake_client([], foundation_models=foundation),
    ):
        result = bedrock_models.list_bedrock_models(provider="all")
    ids = [m["modelId"] for m in result["models"]]
    assert "amazon.callable-v1:0" in ids
    assert "anthropic.future-profile-only-v1:0" not in ids


def test_foundation_model_dedup_against_inference_profile():
    """If a model exists as a us.* inference profile, its bare foundation-model
    entry must not be duplicated in the output."""
    pages = [[("us.anthropic.claude-sonnet-4-6", "US Claude Sonnet 4.6")]]
    foundation = [("anthropic.claude-sonnet-4-6", "Claude Sonnet 4.6")]
    with patch.object(
        bedrock_models,
        "create_boto3_bedrock_client",
        return_value=_fake_client(pages, foundation_models=foundation),
    ):
        result = bedrock_models.list_bedrock_models(provider="anthropic")

    ids = [m["modelId"] for m in result["models"]]
    assert ids.count("us.anthropic.claude-sonnet-4-6") == 1
    assert "anthropic.claude-sonnet-4-6" not in ids


# ---------------------------------------------------------------------------
# ID normalization
#
# These were nested closures inside list_bedrock_models, so they couldn't be
# tested at all. They're module-level now — and the regional-prefix tuple was
# missing jp/au/ca, which meant a Japanese or Australian inference profile got
# "jp" reported as its provider and failed to dedupe against its base model.
# ---------------------------------------------------------------------------


def test_strip_regional_prefix_covers_all_aws_regions():
    cases = {
        "us.anthropic.claude-sonnet-4-6": "anthropic.claude-sonnet-4-6",
        "eu.anthropic.claude-sonnet-4-6": "anthropic.claude-sonnet-4-6",
        "apac.anthropic.claude-sonnet-4-6": "anthropic.claude-sonnet-4-6",
        "jp.anthropic.claude-sonnet-4-6": "anthropic.claude-sonnet-4-6",
        "au.anthropic.claude-sonnet-4-6": "anthropic.claude-sonnet-4-6",
        "ca.anthropic.claude-sonnet-4-6": "anthropic.claude-sonnet-4-6",
        "us-gov.anthropic.claude-sonnet-4-6": "anthropic.claude-sonnet-4-6",
        "global.anthropic.claude-sonnet-4-6": "anthropic.claude-sonnet-4-6",
        # Already bare — unchanged.
        "anthropic.claude-sonnet-4-6": "anthropic.claude-sonnet-4-6",
    }
    for raw, expected in cases.items():
        assert bedrock_models.strip_regional_prefix(raw) == expected, raw


def test_strip_regional_prefix_does_not_eat_vendor_names():
    """A vendor whose name isn't a region prefix must survive untouched."""
    assert bedrock_models.strip_regional_prefix("amazon.nova-pro-v1:0") == "amazon.nova-pro-v1:0"
    assert bedrock_models.strip_regional_prefix("writer.palmyra-x5") == "writer.palmyra-x5"


def test_extract_provider_name_skips_regional_prefix():
    assert bedrock_models.extract_provider_name("jp.anthropic.claude-sonnet-4-6") == "anthropic"
    assert bedrock_models.extract_provider_name("openai.gpt-oss-120b-1:0") == "openai"
    assert bedrock_models.extract_provider_name("nonsense") == "nonsense"
    assert bedrock_models.extract_provider_name("") == "unknown"


def test_normalize_model_id_strips_version_suffix():
    """Converse IDs carry a '-N:M' version that Mantle IDs omit.

    Without stripping it, `openai.gpt-oss-120b-1:0` (Converse) and
    `openai.gpt-oss-120b` (Mantle) look like different models and the same
    weights get listed twice under two different provider prefixes.
    """
    assert bedrock_models.normalize_model_id("openai.gpt-oss-120b-1:0") == "openai.gpt-oss-120b"
    assert (
        bedrock_models.normalize_model_id("us.meta.llama3-3-70b-instruct-v1:0")
        == "meta.llama3-3-70b-instruct-v1:0"
    )
    # No version suffix — unchanged.
    assert bedrock_models.normalize_model_id("openai.gpt-oss-120b") == "openai.gpt-oss-120b"


def test_mantle_duplicate_of_converse_model_is_dropped():
    """gpt-oss is on BOTH endpoints; only the Converse entry should be listed.

    Converse is what validate_providers smoke-tests and what pricing resolves
    against, so it's the entry that should win.
    """
    foundation = [("openai.gpt-oss-120b-1:0", "gpt-oss-120b")]
    mantle = [
        {"id": "openai/bedrock/gpt-oss-120b", "name": "GPT-OSS 120B"},
        {"id": "openai/bedrock/gpt-5.6-sol", "name": "GPT-5.6 Sol"},
    ]
    with patch.object(
        bedrock_models,
        "create_boto3_bedrock_client",
        return_value=_fake_client([], foundation_models=foundation),
    ), patch.object(bedrock_models, "get_external_models", return_value=mantle), patch.object(
        bedrock_models, "detect_available_providers", return_value=[]
    ):
        result = bedrock_models.list_available_models()

    ids = [m["id"] for m in result["models"]]
    assert "bedrock/openai.gpt-oss-120b-1:0" in ids
    assert "openai/bedrock/gpt-oss-120b" not in ids, "duplicate of a Converse model"
    # A Mantle-only model must still come through.
    assert "openai/bedrock/gpt-5.6-sol" in ids
