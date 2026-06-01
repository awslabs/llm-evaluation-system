"""Headline aggregation across scorers (eval_results._mean_scores).

The viewer headline for a multi-scorer run with no jury must reflect EVERY
scorer, not just whichever came first. Regression test for the bug where a
RAG run (faithfulness + answer_relevancy) showed only faithfulness as the
overall score.
"""

from __future__ import annotations

from eval_mcp.core.eval_results import _mean_scores


def test_mean_of_two_scorers():
    # faithfulness 0.79 + answer_relevancy 0.95 → 0.87, not 0.79.
    assert _mean_scores({"faithfulness": 0.79, "answer_relevancy": 0.95}) == 0.87


def test_mean_single_scorer_is_itself():
    assert _mean_scores({"f1": 0.47}) == 0.47


def test_mean_empty_is_zero():
    assert _mean_scores({}) == 0.0


def test_mean_is_unweighted():
    # Every scorer contributes equally regardless of insertion order.
    a = _mean_scores({"x": 0.0, "y": 1.0})
    b = _mean_scores({"y": 1.0, "x": 0.0})
    assert a == b == 0.5
