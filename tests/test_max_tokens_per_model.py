"""The generation ceiling is resolved per model, not hardcoded.

A single `--max-tokens` number cannot serve both Bedrock endpoints, because they
fail in opposite directions:

  * Converse (`bedrock/<id>`) NEEDS a value — Inspect's global default is 2048
    (inspect_ai/_util/constants.py), which truncates real answers. Worse for
    reasoning models: they can spend the whole budget on the reasoning channel
    and return an EMPTY completion with stop_reason="max_tokens". The sample
    still "completes", so it scores 0 and the run reports success — the model
    that reasoned hardest looks like the worst.
  * Mantle (`openai/bedrock/<id>`) needs NO value. Omit it and the model runs to
    its own limit; measured against the live API, gpt-5.6-sol produced 10,816
    tokens on a long-form prompt where an 8192 cap truncated it.

Nor does one high number work, since exceeding a model's limit is a hard
ValidationException rather than a clamp ("The maximum tokens you requested
exceeds the model limit of 10000"). Verified live: Nova Pro caps at 10,000,
Claude Haiku 4.5 at 64,000, gpt-oss-20b at 128,000 — each accepts exactly its
advertised value and rejects one above it.

So the limit comes from the model itself (LiteLLM's dataset, the same source
that backs pricing — AWS does not expose output limits via GetFoundationModel).
"""
from __future__ import annotations

import pytest

from eval_mcp.tools.run_eval import _MAX_TOKENS_FALLBACK, _max_tokens_for_run


MANTLE = "openai/bedrock/gpt-5.6-terra"
NOVA = "bedrock/us.amazon.nova-pro-v1:0"          # advertised 10_000
HAIKU = "bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0"  # advertised 64_000
UNKNOWN = "bedrock/some.model-that-does-not-exist-v9:0"


def test_mantle_only_run_omits_the_flag():
    """The whole point of the fix: no ceiling for Mantle-only runs.

    Passing one is what created the empty-completion truncation this change
    exists to remove.
    """
    assert _max_tokens_for_run([MANTLE]) is None
    assert _max_tokens_for_run([MANTLE, "openai/bedrock/gpt-5.6-sol"]) is None


def test_converse_run_uses_the_models_own_limit():
    """Converse needs a value, and it must be the model's real maximum — not a
    constant, or we're back to truncating whatever the constant is below."""
    assert _max_tokens_for_run([NOVA]) == 10_000
    assert _max_tokens_for_run([HAIKU]) == 64_000


def test_mixed_run_takes_the_lowest_limit():
    """`--max-tokens` is global (targets share one `--model` flag), so a mixed
    run has to satisfy its most restrictive model.

    Erring low truncates a tail; erring high is a ValidationException that
    kills every sample for the smaller-limit model. Those are not symmetric,
    which is why this is min() and not max().
    """
    assert _max_tokens_for_run([NOVA, HAIKU]) == 10_000
    assert _max_tokens_for_run([HAIKU, NOVA]) == 10_000


def test_mixed_mantle_and_converse_still_bounded_by_converse():
    """A Mantle model in the run doesn't remove the need for a ceiling — the
    Converse model alongside it would otherwise fall back to Inspect's 2048."""
    assert _max_tokens_for_run([MANTLE, NOVA]) == 10_000


def test_unknown_model_falls_back_rather_than_guessing():
    """An unrecognised model means "can't reason about it", not "unlimited".
    Omitting the flag would hand it Inspect's 2048."""
    assert _max_tokens_for_run([UNKNOWN]) == _MAX_TOKENS_FALLBACK


def test_lowest_limit_is_never_clamped_up_to_the_fallback():
    """Regression guard on a bug in the first draft of this fix.

    Clamping the minimum up to the fallback (`max(min(limits), FALLBACK)`) reads
    like a sensible floor, but it pushes the value ABOVE a real API bound
    whenever a model's limit is below the fallback — converting a graceful
    truncation into the hard rejection this whole change is meant to avoid.
    """
    assert _max_tokens_for_run([NOVA]) == 10_000 < _MAX_TOKENS_FALLBACK * 2
    # Explicitly: a model whose advertised limit is under the fallback must NOT
    # be raised to it.
    limit = _max_tokens_for_run([NOVA])
    assert limit == 10_000, (
        f"expected Nova's advertised 10000, got {limit} — a clamp to the "
        f"fallback would make Bedrock reject the request outright"
    )


def test_score_only_run_omits_the_flag():
    """No models means nothing generates; a ceiling is meaningless."""
    assert _max_tokens_for_run([]) is None


def test_max_output_tokens_lookup_resolves_bedrock_id_shapes():
    """The limit lookup has to survive the ID shapes we actually pass.

    Region-prefixed Converse IDs, `bedrock/` prefixes and Mantle's
    `openai/bedrock/` all have to resolve, or the fix silently degrades to the
    fallback for every model.
    """
    from eval_mcp.core.pricing import get_max_output_tokens

    for model_id in (NOVA, HAIKU, "bedrock/openai.gpt-oss-20b-1:0", MANTLE):
        limit = get_max_output_tokens(model_id)
        assert isinstance(limit, int) and limit > 0, (
            f"{model_id} did not resolve to a positive limit (got {limit!r}); "
            f"check _candidates() normalisation in pricing.py"
        )


def test_generated_scorer_source_is_valid_python():
    """`JURY_SCORER_BLOCK` reaches evals as SOURCE TEXT inside the generated
    config, so a syntax error there breaks every eval rather than failing a
    test. Cheap insurance whenever that block is edited."""
    import ast

    from eval_mcp.tools.create_config import JURY_SCORER_BLOCK

    ast.parse(JURY_SCORER_BLOCK)


def test_partial_truncation_is_flagged_in_the_generated_scorer():
    """A cut-off answer used to be scored as though it were complete.

    It still gets scored (partial output carries real signal), but the score is
    a floor rather than a measurement — completeness criteria fail on a severed
    answer regardless of quality — so it has to be visibly marked.
    """
    from eval_mcp.tools.create_config import JURY_SCORER_BLOCK

    assert "truncated_partial" in JURY_SCORER_BLOCK
    assert "truncated_partial_output" in JURY_SCORER_BLOCK, (
        "the metadata flag is what downstream readers filter on"
    )
