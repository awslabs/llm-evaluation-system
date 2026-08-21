"""Tests for EVAL_MCP_AIWF_TURNS_FILE — the aiwf user-input override.

The override exists to score the benchmark on inputs that did not come from a
keyboard (STT transcripts), which is how you pick a model to sit behind a
speech pipeline. Its whole safety property is that ONLY ``input`` may differ:
if a caller can edit the goldens or the expected tool calls, the run silently
scores a different task while still reporting the benchmark's name and metric.
These tests pin that boundary.

Deterministic and offline — no model calls.
"""

import json

import pytest

from eval_mcp.benchmarks.aiwf import data_loader as dl

ENV = "EVAL_MCP_AIWF_TURNS_FILE"


@pytest.fixture(autouse=True)
def _clear_cache(monkeypatch):
    """turns() is lru_cached, so every test must start from a cold cache."""
    dl.turns.cache_clear()
    monkeypatch.delenv(ENV, raising=False)
    yield
    dl.turns.cache_clear()


def _vendored() -> list[dict]:
    return json.loads((dl._DATA / "turns.json").read_text(encoding="utf-8"))


def _write(tmp_path, rows, name="turns.json"):
    p = tmp_path / name
    p.write_text(json.dumps(rows), encoding="utf-8")
    return str(p)


def test_no_override_uses_vendored_script():
    got = dl.turns()
    want = _vendored()
    assert len(got) == len(want)
    assert [t.input for t in got] == [r["input"] for r in want]


def test_override_swaps_inputs_and_keeps_goldens(tmp_path, monkeypatch):
    rows = _vendored()
    for r in rows:
        r["input"] = f"transcribed {r['index']}"
    monkeypatch.setenv(ENV, _write(tmp_path, rows))

    got = dl.turns()
    want = _vendored()
    assert [t.input for t in got] == [f"transcribed {i}" for i in range(len(want))]
    # the scored targets must be untouched
    assert [t.golden_text for t in got] == [r["golden_text"] for r in want]
    assert [t.required_function_call for t in got] == [
        r["required_function_call"] for r in want
    ]


def test_edited_golden_text_is_rejected(tmp_path, monkeypatch):
    rows = _vendored()
    rows[3]["golden_text"] = "a different expected answer"
    monkeypatch.setenv(ENV, _write(tmp_path, rows))
    with pytest.raises(ValueError, match="golden_text edited on turn 3"):
        dl.turns()


def test_edited_tool_call_is_rejected(tmp_path, monkeypatch):
    rows = _vendored()
    target = next(r for r in rows if r["required_function_call"])
    target["required_function_call"] = {"name": "something_else", "args": {}}
    monkeypatch.setenv(ENV, _write(tmp_path, rows))
    with pytest.raises(ValueError, match="required_function_call edited"):
        dl.turns()


def test_dropped_turn_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, _write(tmp_path, _vendored()[:-1]))
    with pytest.raises(ValueError, match="expected 30 turns, got 29"):
        dl.turns()


def test_reordered_turns_are_rejected(tmp_path, monkeypatch):
    rows = _vendored()
    rows[0], rows[1] = rows[1], rows[0]
    monkeypatch.setenv(ENV, _write(tmp_path, rows))
    with pytest.raises(ValueError, match="turn order changed"):
        dl.turns()


def test_missing_file_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, str(tmp_path / "nope.json"))
    with pytest.raises(FileNotFoundError, match=ENV):
        dl.turns()
