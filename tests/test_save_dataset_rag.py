"""Tests for save_dataset's retrieval_context handling.

Covers normalisation (list/JSON string/pipe-separator), end-to-end
column flow through ``rows_to_test_cases``, and the "missing column"
silent-pass behaviour.
"""

import pytest

from eval_mcp.tools.save_dataset import (
    _coerce_retrieval_context,
    rows_to_test_cases,
)


def test_coerce_passthrough_list() -> None:
    assert _coerce_retrieval_context(["a", "b"]) == ["a", "b"]


def test_coerce_drops_empty_strings_in_list() -> None:
    # "" entries are noise — drop them, but keep order of the rest.
    assert _coerce_retrieval_context(["a", "", "b"]) == ["a", "b"]


def test_coerce_rejects_non_string_list() -> None:
    with pytest.raises(ValueError, match="only strings"):
        _coerce_retrieval_context(["a", 42])


def test_coerce_parses_json_string() -> None:
    assert _coerce_retrieval_context('["a","b"]') == ["a", "b"]


def test_coerce_parses_pipe_separator() -> None:
    assert _coerce_retrieval_context("a ||| b ||| c") == ["a", "b", "c"]


def test_coerce_bare_string_is_single_chunk() -> None:
    assert _coerce_retrieval_context("a single chunk") == ["a single chunk"]


def test_coerce_empty_string_is_none() -> None:
    assert _coerce_retrieval_context("") is None
    assert _coerce_retrieval_context("   ") is None


def test_coerce_none() -> None:
    assert _coerce_retrieval_context(None) is None


def test_coerce_invalid_json_raises_descriptive_error() -> None:
    with pytest.raises(ValueError, match="JSON"):
        _coerce_retrieval_context('["a", "b"')  # missing close


def test_rows_to_test_cases_omits_retrieval_context_when_unmapped() -> None:
    rows = [{"q": "What?", "a": "Answer.", "rc": '["x"]'}]
    cases = rows_to_test_cases(rows, "q", "a")
    assert cases == [{"vars": {"question": "What?", "golden_answer": "Answer."}}]


def test_rows_to_test_cases_includes_retrieval_context_when_mapped() -> None:
    rows = [{"q": "Q1?", "a": "A1.", "rc": ["chunk one", "chunk two"]}]
    cases = rows_to_test_cases(rows, "q", "a", "rc")
    assert cases == [
        {
            "vars": {
                "question": "Q1?",
                "golden_answer": "A1.",
                "retrieval_context": ["chunk one", "chunk two"],
            }
        }
    ]


def test_rows_to_test_cases_drops_missing_rc_silently_when_mapped() -> None:
    # Some rows have retrieval_context, some don't. The save step keeps
    # the dataset writable; downstream RAG-scorer creation fails fast
    # only if a RAG scorer is selected and a sample has no rc — that
    # check lives in create_config, not here.
    rows = [
        {"q": "Q1?", "a": "A1.", "rc": ["one"]},
        {"q": "Q2?", "a": "A2.", "rc": ""},
        {"q": "Q3?", "a": "A3."},
    ]
    cases = rows_to_test_cases(rows, "q", "a", "rc")
    assert len(cases) == 3
    assert "retrieval_context" in cases[0]["vars"]
    assert "retrieval_context" not in cases[1]["vars"]
    assert "retrieval_context" not in cases[2]["vars"]
