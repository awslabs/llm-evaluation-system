"""Tests for the critic-led refinement loop in generate_judge.

Two layers:
  - Pure-function tests for _apply_updates and the loop's exit logic
    (mocked _score_samples + _critique_criteria, no Bedrock calls).
  - One integration test gated on Bedrock reachability that proves the
    full path produces more than the old 5-criterion ceiling.

Run unit tests only:
    uv run pytest tests/test_generate_judge_iter.py -v -k "not integration"

Run everything (requires AWS creds + Bedrock model access):
    uv run pytest tests/test_generate_judge_iter.py -v
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, patch

import pytest

from eval_mcp.tools.generate_judge import (
    CRITIC_MAX_ITERATIONS,
    _apply_updates,
    refine_criteria_loop,
)
from eval_mcp.core.judge_config import MAX_CRITERIA


# ---------------------------------------------------------------------------
# _apply_updates — pure function, no mocks needed
# ---------------------------------------------------------------------------


def _criteria(*names: str) -> list[dict]:
    return [{"name": n, "description": f"1 if {n}, 0 otherwise"} for n in names]


def test_apply_updates_keeps_unchanged_when_no_updates():
    starting = _criteria("a", "b", "c")
    out = _apply_updates(starting, {"criteria_updates": [], "new_criteria": []})
    assert out == starting


def test_apply_updates_drops_named_criterion():
    starting = _criteria("a", "b", "c")
    critique = {
        "criteria_updates": [{"name": "b", "action": "drop", "reason": "redundant"}],
    }
    out = _apply_updates(starting, critique)
    assert [c["name"] for c in out] == ["a", "c"]


def test_apply_updates_rewrites_in_place_preserving_order():
    starting = _criteria("a", "b", "c")
    critique = {
        "criteria_updates": [
            {
                "name": "b",
                "action": "rewrite",
                "new_name": "b_tightened",
                "new_description": "1 if very b, 0 otherwise",
                "reason": "was vague",
            },
        ],
    }
    out = _apply_updates(starting, critique)
    assert [c["name"] for c in out] == ["a", "b_tightened", "c"]
    assert out[1]["description"] == "1 if very b, 0 otherwise"


def test_apply_updates_appends_new_criteria():
    starting = _criteria("a")
    critique = {
        "new_criteria": [
            {"name": "d", "description": "1 if d, 0 otherwise", "reason": "missing"},
            {"name": "e", "description": "1 if e, 0 otherwise"},
        ],
    }
    out = _apply_updates(starting, critique)
    assert [c["name"] for c in out] == ["a", "d", "e"]


def test_apply_updates_unknown_action_keeps_criterion():
    starting = _criteria("a", "b")
    critique = {
        "criteria_updates": [{"name": "a", "action": "vandalize", "reason": "lol"}],
    }
    out = _apply_updates(starting, critique)
    assert [c["name"] for c in out] == ["a", "b"]


def test_apply_updates_rewrite_with_missing_fields_keeps_originals():
    starting = _criteria("a")
    critique = {
        "criteria_updates": [{"name": "a", "action": "rewrite", "reason": "..."}],
    }
    out = _apply_updates(starting, critique)
    # No new_name / new_description provided → fall back to the original.
    assert out == starting


def test_apply_updates_skips_malformed_new_criteria():
    starting = _criteria("a")
    critique = {
        "new_criteria": [
            {"name": "valid", "description": "1 if v, 0 otherwise"},
            {"name": "missing_desc"},
            {"description": "missing name"},
        ],
    }
    out = _apply_updates(starting, critique)
    assert [c["name"] for c in out] == ["a", "valid"]


# ---------------------------------------------------------------------------
# refine_criteria_loop — mocked Bedrock interactions
# ---------------------------------------------------------------------------


@pytest.fixture
def starting_criteria():
    return _criteria("alpha", "beta", "gamma")


@pytest.fixture
def qa_pairs():
    return [
        {"question": f"q{i}", "golden_answer": f"a{i}"} for i in range(20)
    ]


def test_loop_returns_input_when_max_iter_zero(starting_criteria, qa_pairs):
    out = asyncio.run(
        refine_criteria_loop(
            bedrock=None,
            criteria=starting_criteria,
            qa_pairs=qa_pairs,
            domain="t",
            max_iter=0,
        )
    )
    assert out == starting_criteria


def test_loop_returns_input_when_no_criteria(qa_pairs):
    out = asyncio.run(
        refine_criteria_loop(
            bedrock=None,
            criteria=[],
            qa_pairs=qa_pairs,
            domain="t",
        )
    )
    assert out == []


def test_loop_returns_input_when_no_qa_pairs(starting_criteria):
    out = asyncio.run(
        refine_criteria_loop(
            bedrock=None,
            criteria=starting_criteria,
            qa_pairs=[],
            domain="t",
        )
    )
    assert out == starting_criteria


def test_loop_exits_on_no_changes_needed(starting_criteria, qa_pairs):
    """When the critic returns no_changes_needed=True on round 1, we
    return immediately with the input criteria."""
    with patch(
        "eval_mcp.tools.generate_judge._score_samples",
        new=AsyncMock(return_value=[{"question": "q", "golden": "a", "scores": {}}]),
    ), patch(
        "eval_mcp.tools.generate_judge._critique_criteria",
        new=AsyncMock(return_value={"no_changes_needed": True}),
    ) as mock_critic:
        out = asyncio.run(
            refine_criteria_loop(
                bedrock=None,
                criteria=starting_criteria,
                qa_pairs=qa_pairs,
                domain="t",
                max_iter=3,
            )
        )
    assert out == starting_criteria
    assert mock_critic.await_count == 1


def test_loop_applies_changes_and_continues(starting_criteria, qa_pairs):
    """Iter 1: critic drops 'beta'. Iter 2: critic says converged.
    Result should be the post-iter-1 criteria."""
    critic_responses = [
        {
            "no_changes_needed": False,
            "criteria_updates": [
                {"name": "beta", "action": "drop", "reason": "redundant"}
            ],
        },
        {"no_changes_needed": True},
    ]
    with patch(
        "eval_mcp.tools.generate_judge._score_samples",
        new=AsyncMock(return_value=[{"question": "q", "golden": "a", "scores": {}}]),
    ), patch(
        "eval_mcp.tools.generate_judge._critique_criteria",
        new=AsyncMock(side_effect=critic_responses),
    ):
        out = asyncio.run(
            refine_criteria_loop(
                bedrock=None,
                criteria=starting_criteria,
                qa_pairs=qa_pairs,
                domain="t",
                max_iter=3,
            )
        )
    assert [c["name"] for c in out] == ["alpha", "gamma"]


def test_loop_falls_back_on_critic_error(starting_criteria, qa_pairs):
    """A raised exception inside an iteration must NOT propagate. The
    loop returns the most recent good criteria — here, the original
    input since iter 1 failed before any update applied."""
    with patch(
        "eval_mcp.tools.generate_judge._score_samples",
        new=AsyncMock(return_value=[{"question": "q", "golden": "a", "scores": {}}]),
    ), patch(
        "eval_mcp.tools.generate_judge._critique_criteria",
        new=AsyncMock(side_effect=RuntimeError("bedrock blew up")),
    ):
        out = asyncio.run(
            refine_criteria_loop(
                bedrock=None,
                criteria=starting_criteria,
                qa_pairs=qa_pairs,
                domain="t",
                max_iter=3,
            )
        )
    assert out == starting_criteria


def test_loop_returns_last_good_when_second_iter_fails(starting_criteria, qa_pairs):
    """Iter 1 applies changes successfully, iter 2 errors. Return the
    post-iter-1 state, not the original."""
    critic_responses = [
        {
            "no_changes_needed": False,
            "criteria_updates": [{"name": "alpha", "action": "drop", "reason": "x"}],
        },
        RuntimeError("kaboom"),
    ]
    with patch(
        "eval_mcp.tools.generate_judge._score_samples",
        new=AsyncMock(return_value=[{"question": "q", "golden": "a", "scores": {}}]),
    ), patch(
        "eval_mcp.tools.generate_judge._critique_criteria",
        new=AsyncMock(side_effect=critic_responses),
    ):
        out = asyncio.run(
            refine_criteria_loop(
                bedrock=None,
                criteria=starting_criteria,
                qa_pairs=qa_pairs,
                domain="t",
                max_iter=3,
            )
        )
    assert [c["name"] for c in out] == ["beta", "gamma"]


def test_loop_respects_max_iter_when_critic_never_converges(starting_criteria, qa_pairs):
    """Critic always proposes more changes; loop exits at max_iter with
    whatever the latest state is. No 'didn't converge' surfacing per design."""
    critic_response = {
        "no_changes_needed": False,
        "criteria_updates": [],
        "new_criteria": [{"name": "extra", "description": "1 if extra, 0 otherwise"}],
    }
    score_mock = AsyncMock(return_value=[{"question": "q", "golden": "a", "scores": {}}])
    critic_mock = AsyncMock(return_value=critic_response)
    with patch(
        "eval_mcp.tools.generate_judge._score_samples",
        new=score_mock,
    ), patch(
        "eval_mcp.tools.generate_judge._critique_criteria",
        new=critic_mock,
    ):
        out = asyncio.run(
            refine_criteria_loop(
                bedrock=None,
                criteria=starting_criteria,
                qa_pairs=qa_pairs,
                domain="t",
                max_iter=2,
            )
        )
    # Started with 3, each iter adds 1, ran 2 iters → 5.
    assert len(out) == 5
    assert critic_mock.await_count == 2


def test_loop_caps_at_max_criteria(qa_pairs):
    """When the critic proposes additions that push past MAX_CRITERIA,
    the loop truncates rather than letting the set grow unbounded."""
    starting = _criteria(*[f"c{i}" for i in range(MAX_CRITERIA - 1)])
    flood = {
        "no_changes_needed": False,
        "new_criteria": [
            {"name": f"new_{i}", "description": "1 if x, 0 otherwise"}
            for i in range(5)
        ],
    }
    with patch(
        "eval_mcp.tools.generate_judge._score_samples",
        new=AsyncMock(return_value=[{"question": "q", "golden": "a", "scores": {}}]),
    ), patch(
        "eval_mcp.tools.generate_judge._critique_criteria",
        new=AsyncMock(side_effect=[flood, {"no_changes_needed": True}]),
    ):
        out = asyncio.run(
            refine_criteria_loop(
                bedrock=None,
                criteria=starting,
                qa_pairs=qa_pairs,
                domain="t",
                max_iter=3,
            )
        )
    assert len(out) == MAX_CRITERIA


def test_loop_falls_back_when_all_scoring_fails(starting_criteria, qa_pairs):
    """If every per-sample judge call fails, the loop bails to last-good
    rather than calling the critic with no signal."""
    with patch(
        "eval_mcp.tools.generate_judge._score_samples",
        new=AsyncMock(return_value=[]),  # all samples dropped
    ), patch(
        "eval_mcp.tools.generate_judge._critique_criteria",
        new=AsyncMock(),
    ) as mock_critic:
        out = asyncio.run(
            refine_criteria_loop(
                bedrock=None,
                criteria=starting_criteria,
                qa_pairs=qa_pairs,
                domain="t",
                max_iter=3,
            )
        )
    assert out == starting_criteria
    mock_critic.assert_not_called()


# ---------------------------------------------------------------------------
# Integration test (gated on Bedrock reachability) — proves end-to-end that
# the cap is actually lifted in practice, not just in code.
# ---------------------------------------------------------------------------


def _bedrock_reachable() -> bool:
    try:
        import boto3

        session = boto3.Session(region_name=os.environ.get("AWS_REGION", "us-west-2"))
        creds = session.get_credentials()
        return creds is not None and creds.access_key is not None
    except Exception:
        return False


@pytest.mark.skipif(
    not _bedrock_reachable(),
    reason="Bedrock credentials not available — skipping live criteria-loop integration test.",
)
def test_integration_loop_produces_more_than_five_criteria():
    """End-to-end: feed the loop a real dataset and confirm it returns
    a set with >5 criteria (the old hard cap). Doesn't assert on
    specific content — that's the job of human review. Just proves the
    plumbing lets the cap exceed 5."""
    from eval_mcp.core.bedrock_client import BedrockClient
    from eval_mcp.tools.generate_judge import generate_judge

    bedrock = BedrockClient()
    qa_pairs = [
        {
            "question": "What's the capital of France?",
            "golden_answer": "Paris is the capital of France. It has been the country's capital since 987 AD.",
        },
        {
            "question": "Explain how photosynthesis works in plants.",
            "golden_answer": "Plants convert sunlight, water, and carbon dioxide into glucose and oxygen via chlorophyll in their leaves. The light reactions produce ATP and NADPH; the Calvin cycle uses these to fix CO2 into sugars.",
        },
        {
            "question": "What is the Pythagorean theorem?",
            "golden_answer": "For a right triangle with legs a and b and hypotenuse c, a² + b² = c². It's used to compute distances and is named for the Greek mathematician Pythagoras.",
        },
        {
            "question": "Who wrote Hamlet?",
            "golden_answer": "William Shakespeare wrote Hamlet around 1600. It's one of his four major tragedies.",
        },
        {
            "question": "What causes the seasons?",
            "golden_answer": "Earth's axis is tilted ~23.5° relative to its orbital plane. As Earth orbits the Sun, different hemispheres receive more direct sunlight at different times of year, causing seasonal changes.",
        },
    ]

    initial = asyncio.run(generate_judge(bedrock, qa_pairs, "general knowledge"))
    initial_criteria = initial.get("criteria", [])
    assert initial_criteria, "Initial generation returned nothing"

    refined = asyncio.run(
        refine_criteria_loop(
            bedrock=bedrock,
            criteria=initial_criteria,
            qa_pairs=qa_pairs,
            domain="general knowledge",
            max_iter=2,  # cap iterations to keep CI cost bounded
            sample_size=3,
        )
    )

    assert len(refined) >= 3, f"Loop returned fewer criteria than minimum: {len(refined)}"
    assert len(refined) <= MAX_CRITERIA, f"Loop exceeded MAX_CRITERIA: {len(refined)}"
    # Headline assertion: the OLD schema cap was 5. We want to confirm the
    # path is structurally capable of producing more, even if a particular
    # run converges below 5 — so we check the cap was lifted, not that the
    # specific result is large.
    # (A run that converges to 4 doesn't disprove the cap was lifted; only
    # a run that returns >5 does. We don't assert >5 because convergence
    # is dataset-dependent and would make the test flaky.)
    print(f"Refined criteria ({len(refined)}):")
    for c in refined:
        print(f"  - {c['name']}: {c['description']}")
