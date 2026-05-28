"""Tests for save_dataset's actual_output column handling.

Score-only mode is opt-in via the column mapping — when an
``actual_output`` source column is supplied, the test cases capture
the value as ``vars.actual_output``. Empty / missing values are
silently dropped (per-row) and ``create_eval_config`` later refuses
mixed datasets at config-generation time.
"""

from eval_mcp.tools.save_dataset import rows_to_test_cases


def test_omits_actual_output_when_column_unmapped() -> None:
    rows = [{"q": "Q?", "a": "A.", "ao": "static answer"}]
    cases = rows_to_test_cases(rows, "q", "a")
    assert cases == [{"vars": {"question": "Q?", "golden_answer": "A."}}]


def test_includes_actual_output_when_column_mapped() -> None:
    rows = [{"q": "Q1?", "a": "A1.", "ao": "the model said: A1."}]
    cases = rows_to_test_cases(rows, "q", "a", "ao")
    assert cases == [
        {
            "vars": {
                "question": "Q1?",
                "golden_answer": "A1.",
                "actual_output": "the model said: A1.",
            }
        }
    ]


def test_drops_empty_actual_output_per_row() -> None:
    # Empty cells should NOT become actual_output="" — they're dropped
    # so create_eval_config's mixed-dataset detection can see them as
    # missing rather than as a present-but-empty signal.
    rows = [
        {"q": "Q1?", "a": "A1.", "ao": "answer one"},
        {"q": "Q2?", "a": "A2.", "ao": ""},
        {"q": "Q3?", "a": "A3.", "ao": "   "},
        {"q": "Q4?", "a": "A4."},
    ]
    cases = rows_to_test_cases(rows, "q", "a", "ao")
    assert len(cases) == 4
    assert "actual_output" in cases[0]["vars"]
    assert "actual_output" not in cases[1]["vars"]
    assert "actual_output" not in cases[2]["vars"]
    assert "actual_output" not in cases[3]["vars"]


def test_skips_rows_missing_question_or_answer() -> None:
    rows = [
        {"q": "Q1?", "a": "A1.", "ao": "ao1"},
        {"q": "Q2?", "a": "", "ao": "ao2"},      # missing answer
        {"q": "", "a": "A3.", "ao": "ao3"},      # missing question
        {"q": "Q4?", "a": "A4.", "ao": "ao4"},
    ]
    cases = rows_to_test_cases(rows, "q", "a", "ao")
    # Only Q1 and Q4 survive; Q2/Q3 are dropped because the contract
    # requires both question and golden_answer.
    assert len(cases) == 2
    assert [c["vars"]["question"] for c in cases] == ["Q1?", "Q4?"]
    assert all("actual_output" in c["vars"] for c in cases)
