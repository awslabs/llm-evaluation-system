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
            {"modelId": mid, "modelName": name}
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


def test_supported_models_gate_filters_unknowns():
    """Entries not in SUPPORTED_MODELS must not appear. This is an intentional
    manual-curation gate; loosen with care."""
    pages = [[("us.anthropic.fictional-model-v99", "Fictional")]]
    with patch.object(bedrock_models, "create_boto3_bedrock_client", return_value=_fake_client(pages)):
        result = bedrock_models.list_bedrock_models(provider="anthropic")
    assert result["count"] == 0


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
