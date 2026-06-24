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
