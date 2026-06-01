"""Optimizer multi-scorer support (optimize_prompt).

Covers the deterministic pieces: score coercion, the mean-across-scorers
objective in _extract_rows_from_log, and that the optimizer's failure context
surfaces non-jury scorer reasons. The full loop needs Bedrock + Inspect
subprocesses and is exercised by running the MCP.
"""

from __future__ import annotations

from types import SimpleNamespace

from eval_mcp.tools.optimize_prompt import (
    _coerce_score_value,
    _extract_rows_from_log,
    _format_failures_for_optimizer,
    _pass_rate,
    _select_failures,
)


# ---------------------------------------------------------------------------
# _coerce_score_value
# ---------------------------------------------------------------------------


def test_coerce_numeric_and_string():
    assert _coerce_score_value(0.5) == 0.5
    assert _coerce_score_value(1) == 1.0
    assert _coerce_score_value("0.73") == 0.73


def test_coerce_categorical():
    assert _coerce_score_value("C") == 1.0
    assert _coerce_score_value("I") == 0.0


def test_coerce_none_and_garbage():
    assert _coerce_score_value(None) is None
    assert _coerce_score_value("banana") is None


# ---------------------------------------------------------------------------
# _extract_rows_from_log — sample_score is the MEAN across scorers
# ---------------------------------------------------------------------------


def _score(value, explanation="", metadata=None):
    return SimpleNamespace(value=value, explanation=explanation, metadata=metadata or {})


def _sample(scores: dict, answer="ans", inp="q", target="g"):
    return SimpleNamespace(
        scores=scores,
        input=inp,
        target=target,
        output=SimpleNamespace(completion=answer),
    )


def test_sample_score_is_mean_across_scorers():
    """faithfulness 0.6 + answer_relevancy 1.0 → headline 0.8 (not 0.6)."""
    log = SimpleNamespace(samples=[
        _sample({
            "faithfulness": _score(0.6),
            "answer_relevancy": _score(1.0),
        })
    ])
    rows = _extract_rows_from_log(log)
    assert rows[0]["sample_score"] == 0.8


def test_jury_plus_rag_averaged():
    """jury 0.5 + faithfulness 1.0 → 0.75, combining both signals."""
    log = SimpleNamespace(samples=[
        _sample({
            "jury_scorer": _score(0.5, metadata={"criteria_results": [{"name": "accuracy", "score": 0.5}]}),
            "faithfulness": _score(1.0),
        })
    ])
    rows = _extract_rows_from_log(log)
    assert rows[0]["sample_score"] == 0.75
    # jury criteria still captured for the failure context
    assert rows[0]["criteria_results"][0]["name"] == "accuracy"


def test_scorer_breakdown_captured():
    log = SimpleNamespace(samples=[
        _sample({"faithfulness": _score(0.3, explanation="contradicts chunk 2")})
    ])
    rows = _extract_rows_from_log(log)
    bd = rows[0]["scorer_breakdown"]
    assert bd["faithfulness"]["score"] == 0.3
    assert "contradicts" in bd["faithfulness"]["explanation"]


def test_no_scores_is_zero():
    log = SimpleNamespace(samples=[_sample({})])
    rows = _extract_rows_from_log(log)
    assert rows[0]["sample_score"] == 0.0


# ---------------------------------------------------------------------------
# Failure context surfaces non-jury scorer reasons
# ---------------------------------------------------------------------------


def test_failure_context_shows_rag_reason():
    failures = [{
        "question": "What year?",
        "golden": "1889",
        "answer": "1900",
        "criteria_results": [],  # no jury
        "scorer_breakdown": {
            "faithfulness": {"score": 0.0, "explanation": "answer 1900 contradicts the context (1889)"},
            "answer_relevancy": {"score": 1.0, "explanation": ""},
        },
    }]
    out = _format_failures_for_optimizer(failures)
    assert "Low scorers:" in out
    assert "faithfulness: 0.00" in out
    assert "contradicts the context" in out
    # answer_relevancy passed (1.0) → not listed as a low scorer
    assert "answer_relevancy" not in out


def test_failure_context_prefers_jury_criteria():
    failures = [{
        "question": "q",
        "golden": "g",
        "answer": "a",
        "criteria_results": [
            {"name": "accuracy", "score": 0.0, "votes_for": 1, "total": 4,
             "improvement_notes": [{"judge": "claude", "note": "missed the date"}]},
        ],
        "scorer_breakdown": {"jury_scorer": {"score": 0.0, "explanation": ""}},
    }]
    out = _format_failures_for_optimizer(failures)
    assert "Failed criteria (jury):" in out
    assert "missed the date" in out


# ---------------------------------------------------------------------------
# Objective plumbing — _pass_rate / _select_failures use the mean score
# ---------------------------------------------------------------------------


def test_pass_rate_and_failure_selection_use_sample_score():
    rows = [
        {"sample_score": 1.0},
        {"sample_score": 0.5},
        {"sample_score": 0.0},
    ]
    assert _pass_rate(rows) == 0.5
    failing = _select_failures(rows)
    # fully-passing row dropped; worst first
    assert [r["sample_score"] for r in failing] == [0.0, 0.5]
