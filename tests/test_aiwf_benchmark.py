"""Tests for the bundled aiwf multi-turn benchmark.

Scope per CLAUDE.md: narrow deterministic logic only — data fidelity against
upstream, judge-output parsing, metric arithmetic. Whether a model actually
scores well is not testable here and is verified by running the benchmark.
"""

import json

import pytest

from eval_mcp.benchmarks.aiwf import data_loader
from eval_mcp.benchmarks.aiwf.data_loader import (
    AIWF_TASKS,
    knowledge_base,
    system_prompt,
    tool_defs,
    turns,
)
from eval_mcp.benchmarks.aiwf.task import (
    DIMENSIONS,
    _JUDGE_SYSTEM_PROMPT,
    _UPSTREAM_JUDGE_PROMPT,
    _extract_judgments,
    _max_turns,
    _rate,
    _render_transcript,
    _render_user_prompt,
    _turn_rate,
    aiwf_long_context,
    aiwf_medium_context,
)


# --- Dataset fidelity vs upstream (kwindla/aiewf-eval) --------------------


def test_thirty_turns():
    assert len(turns()) == 30


def test_turn_indices_are_sequential():
    assert [t.index for t in turns()] == list(range(30))


def test_six_turns_expect_a_tool_call():
    """Upstream has exactly 6 golden function calls, at these turn indices."""
    with_tool = [t.index for t in turns() if t.expected_tool]
    assert with_tool == [11, 12, 15, 17, 24, 29]


def test_expected_tools_match_upstream():
    by_index = {t.index: t.expected_tool for t in turns() if t.expected_tool}
    assert by_index == {
        11: "submit_session_suggestion",
        12: "submit_session_suggestion",
        15: "submit_dietary_request",
        17: "request_tech_support",
        24: "vote_for_session",
        29: "end_session",
    }


def test_expected_args_are_exposed():
    turn = next(t for t in turns() if t.index == 24)
    assert turn.expected_args == {"name": "Jennifer Smith", "session_id": "936902"}
    # end_session takes none.
    assert next(t for t in turns() if t.index == 29).expected_args == {}


def test_turns_without_a_tool_call_have_no_expectation():
    for t in turns():
        if t.index not in (11, 12, 15, 17, 24, 29):
            assert t.expected_tool is None
            assert t.expected_args == {}


def test_every_turn_has_input_and_golden_text():
    for t in turns():
        assert t.input.strip()
        assert t.golden_text.strip()


def test_five_tools_in_upstream_order():
    from inspect_ai.tool._tool_def import ToolDef

    assert [ToolDef(t).name for t in tool_defs()] == [
        "end_session",
        "submit_dietary_request",
        "submit_session_suggestion",
        "vote_for_session",
        "request_tech_support",
    ]


def test_tool_schemas_match_upstream_parameters():
    from inspect_ai.tool._tool_def import ToolDef

    expected = {
        "end_session": ([], []),
        "submit_dietary_request": (
            ["name", "dietary_preference"],
            ["name", "dietary_preference"],
        ),
        "submit_session_suggestion": (
            ["name", "suggestion_text"],
            ["name", "suggestion_text"],
        ),
        "vote_for_session": (["name", "session_id"], ["name", "session_id"]),
        "request_tech_support": (
            ["name", "issue_description"],
            ["name", "issue_description"],
        ),
    }
    for tool in tool_defs():
        d = ToolDef(tool)
        params, required = expected[d.name]
        assert list(d.parameters.properties) == params, d.name
        assert list(d.parameters.required) == required, d.name


@pytest.mark.parametrize("variant", sorted(AIWF_TASKS))
def test_system_prompt_contains_kb_and_tool_docs(variant):
    prompt = system_prompt(variant)
    kb = knowledge_base(variant)
    assert kb in prompt
    # Preamble before the KB, tools section after it. rindex for the tools
    # heading: the preamble also *mentions* "AVAILABLE TOOLS" in its
    # instructions, so the first occurrence isn't the heading.
    assert prompt.index("KNOWLEDGE BASE") < prompt.index(kb)
    assert prompt.index(kb) < prompt.rindex("AVAILABLE TOOLS")


def test_long_variant_has_a_bigger_knowledge_base():
    """The ONLY difference between the two benchmarks is KB size."""
    medium = knowledge_base("aiwf_medium_context")
    long = knowledge_base("aiwf_long_context")
    assert len(long) > len(medium) * 5
    # Same scaffolding either side of it.
    assert system_prompt("aiwf_medium_context").replace(medium, "") == system_prompt(
        "aiwf_long_context"
    ).replace(long, "")


def test_unknown_variant_is_rejected():
    with pytest.raises(ValueError, match="Unknown aiwf variant"):
        knowledge_base("aiwf_enormous_context")


# --- Task construction ----------------------------------------------------


@pytest.mark.parametrize("factory", [aiwf_medium_context, aiwf_long_context])
def test_task_is_one_sample_holding_the_whole_conversation(factory):
    task = factory()
    assert len(task.dataset) == 1
    assert task.dataset[0].metadata["turns"] == 30


def test_task_names_match_the_variant_keys():
    assert aiwf_medium_context().name == "aiwf_medium_context"
    assert aiwf_long_context().name == "aiwf_long_context"
    assert set(AIWF_TASKS) == {"aiwf_medium_context", "aiwf_long_context"}


# --- Turn cap (smoke-run knob) -------------------------------------------


def test_max_turns_unset_means_all_turns(monkeypatch):
    monkeypatch.delenv("EVAL_MCP_AIWF_MAX_TURNS", raising=False)
    assert _max_turns() is None


@pytest.mark.parametrize("raw", ["not-a-number", "0", "-3", ""])
def test_max_turns_ignores_junk(monkeypatch, raw):
    """A bad cap must run the full benchmark, never zero turns."""
    monkeypatch.setenv("EVAL_MCP_AIWF_MAX_TURNS", raw)
    assert _max_turns() is None


def test_max_turns_reads_a_valid_cap(monkeypatch):
    monkeypatch.setenv("EVAL_MCP_AIWF_MAX_TURNS", "4")
    assert _max_turns() == 4


# --- Judge output parsing -------------------------------------------------


class _FakeToolCall:
    def __init__(self, function, arguments):
        self.function = function
        self.arguments = arguments


class _FakeMessage:
    def __init__(self, tool_calls):
        self.tool_calls = tool_calls


class _FakeOutput:
    def __init__(self, tool_calls=None, completion=""):
        self.message = _FakeMessage(tool_calls) if tool_calls is not None else None
        self.completion = completion


def _judgment(turn, tool=True, instr=True, kb=True, reasoning="ok"):
    return {
        "turn": turn,
        "tool_use_correct": tool,
        "instruction_following": instr,
        "kb_grounding": kb,
        "reasoning": reasoning,
    }


def test_extract_judgments_parses_the_tool_call():
    out = _FakeOutput(
        [
            _FakeToolCall(
                "submit_judgments",
                {"judgments": [_judgment(0), _judgment(1, kb=False)]},
            )
        ]
    )
    judgments, err = _extract_judgments(out, 2)
    assert err is None
    assert judgments[0]["kb_grounding"] is True
    assert judgments[1]["kb_grounding"] is False
    assert judgments[1]["tool_use_correct"] is True


def test_extract_judgments_without_a_tool_call_is_an_error():
    judgments, err = _extract_judgments(_FakeOutput(None, "I refuse"), 30)
    assert judgments is None
    assert "No tool call" in err


def test_extract_judgments_ignores_a_different_tool():
    out = _FakeOutput([_FakeToolCall("something_else", {"judgments": [_judgment(0)]})])
    judgments, err = _extract_judgments(out, 1)
    assert judgments is None
    assert "No submit_judgments" in err


def test_extract_judgments_skips_unparseable_entries():
    """A judge that emits one malformed entry shouldn't void the whole run."""
    out = _FakeOutput(
        [
            _FakeToolCall(
                "submit_judgments",
                {"judgments": [_judgment(0), {"no_turn_key": True}, "garbage"]},
            )
        ]
    )
    judgments, err = _extract_judgments(out, 3)
    assert err is None
    assert list(judgments) == [0]


def test_extract_judgments_defaults_missing_dimensions_to_false():
    """Absent dimension == not demonstrated, not silently passed."""
    out = _FakeOutput(
        [_FakeToolCall("submit_judgments", {"judgments": [{"turn": 0}]})]
    )
    judgments, _ = _extract_judgments(out, 1)
    assert all(judgments[0][d] is False for d in DIMENSIONS)


# --- Metric arithmetic ----------------------------------------------------


class _FakeScore:
    def __init__(self, metadata):
        self.metadata = metadata


class _FakeSampleScore:
    def __init__(self, metadata):
        self.score = _FakeScore(metadata)


def test_turn_rate_pools_turns_across_samples():
    """Rates are per-TURN, so a 2-model run pools 60 turns, not 2 samples."""
    scores = [
        _FakeSampleScore({"turns_judged": 30, "turn_pass": 30}),
        _FakeSampleScore({"turns_judged": 30, "turn_pass": 15}),
    ]
    assert _turn_rate(scores, "turn_pass") == pytest.approx(45 / 60)


def test_turn_rate_with_no_turns_is_zero_not_a_crash():
    assert _turn_rate([_FakeSampleScore({})], "turn_pass") == 0.0


def test_rate_guards_zero_denominator():
    assert _rate(0, 0) == 0.0
    assert _rate(3, 4) == 0.75


# --- Transcript rendering (what the judge sees) ---------------------------


def _record(**kw):
    base = {
        "turn": 0,
        "user_text": "when are the workshops?",
        "assistant_text": "Tuesday, June 3rd.",
        "tool_calls": [],
        "expected_function": None,
        "golden_text": "Workshop day is Tuesday, June 3rd.",
        "stop_reason": "stop",
    }
    base.update(kw)
    return base


def test_transcript_omits_the_knowledge_base():
    """Upstream's judge doesn't get the KB; including it would change what
    kb_grounding measures (and add ~92K tokens per judge call on long)."""
    rendered = _render_transcript([_record()])
    kb = knowledge_base("aiwf_medium_context")
    assert kb not in rendered
    # A distinctive KB line must not leak in either.
    assert kb.splitlines()[10].strip() not in rendered


def test_transcript_includes_golden_and_expected_function():
    rendered = _render_transcript(
        [
            _record(
                turn=24,
                expected_function={
                    "name": "vote_for_session",
                    "args": {"name": "Jennifer Smith", "session_id": "936902"},
                },
                tool_calls=[{"name": "vote_for_session", "args": {"session_id": "936902"}}],
            )
        ]
    )
    assert "## Turn 24" in rendered
    assert "Workshop day is Tuesday" in rendered
    assert "vote_for_session" in rendered
    assert "**Expected Function**" in rendered


def test_transcript_marks_no_expected_function_as_none():
    assert "**Expected Function**: none" in _render_transcript([_record()])


def test_transcript_flags_truncation():
    rendered = _render_transcript([_record(stop_reason="max_tokens")])
    assert "TRUNCATED" in rendered


def test_transcript_hides_the_recovery_attempt():
    """Upstream's judge SKIPS recovery records, so the nudge reply must not
    reach the judge — crediting it would let a model that only complied after
    being prodded look like it complied immediately."""
    rendered = _render_transcript(
        [
            _record(
                recovery_used=True,
                recovery_assistant_text="Sure, submitting that now.",
                recovery_tool_calls=[{"name": "submit_dietary_request", "args": {}}],
            )
        ]
    )
    assert "Please go ahead." not in rendered
    assert "Sure, submitting that now." not in rendered
    assert "submit_dietary_request" not in rendered


def test_transcript_leads_with_the_expected_calls_summary():
    """Upstream's format_turns_for_claude opens with this header."""
    rendered = _render_transcript(
        [
            _record(
                turn=24,
                expected_function={
                    "name": "vote_for_session",
                    "args": {"name": "Jennifer Smith", "session_id": "936902"},
                },
            )
        ]
    )
    assert rendered.startswith("# Expected Function Calls Summary")
    assert '- Turn 24: vote_for_session({"name": "Jennifer Smith"' in rendered
    assert "# Conversation Turns" in rendered


def test_user_prompt_carries_upstream_closing_instructions():
    """The two-phase instructions and "Remember" recap are part of upstream's
    rubric, not decoration — an earlier version of this port dropped them."""
    prompt = _render_user_prompt([_record(), _record(turn=1)])
    assert "Please perform your two-phase evaluation:" in prompt
    assert "MUST contain exactly 2 entries" in prompt
    assert "Be generous with kb_grounding unless there's a clear factual error" in prompt


# --- Judge prompt fidelity vs upstream -----------------------------------


def test_judge_prompt_differs_from_upstream_only_by_the_audio_dimension():
    """The judge prompt IS upstream's, minus the audio-only turn_taking
    dimension. Pinned line-by-line: paraphrasing the rubric silently changes
    what the benchmark measures, which happened once already."""
    import difflib

    # The turn_taking dimension block, verbatim from upstream — every line of
    # it is expected to disappear.
    up_lines = _UPSTREAM_JUDGE_PROMPT.splitlines()
    start = next(i for i, l in enumerate(up_lines)
                 if l.strip().startswith("1. **turn_taking**"))
    end = next(i for i in range(start + 1, len(up_lines))
               if up_lines[i].strip().startswith("2. **"))
    audio_block = {l.strip() for l in up_lines[start:end] if l.strip()}

    removed, added = [], []
    for line in difflib.unified_diff(
        up_lines, _JUDGE_SYSTEM_PROMPT.splitlines(), lineterm="", n=0,
    ):
        if line.startswith(("---", "+++", "@@")):
            continue
        (removed if line.startswith("-") else added).append(line[1:])

    # Every removed line is either part of that block, mentions audio/
    # turn_taking, or is a renumbering artefact.
    for line in removed:
        s = line.strip()
        assert (
            s in audio_block
            or "turn_taking" in s
            or "turn-taking" in s.lower()
            or "audio" in s.lower()
            or s == "For each turn, evaluate FOUR dimensions:"
            or s.startswith(("2. **tool_use_correct**",
                             "3. **instruction_following**",
                             "4. **kb_grounding**"))
            or not s
        ), f"unexpected removal from upstream prompt: {line!r}"

    # Every added line is a renumbering or the THREE-dimensions count.
    for line in added:
        assert line.strip().startswith(("1. **tool_use_correct**",
                                        "2. **instruction_following**",
                                        "3. **kb_grounding**",
                                        '{"turn": 0, "reasoning"')) or line.strip() == (
            "For each turn, evaluate THREE dimensions:"
        ), f"unexpected addition to upstream prompt: {line!r}"


def test_judge_prompt_keeps_upstream_structure():
    """Sections an earlier rewrite had dropped."""
    for expected in (
        "# Two-Phase Evaluation Process",
        "## PHASE 1: Initial Turn-by-Turn Analysis",
        "## PHASE 2: Realignment Analysis",
        "# Critical: Detecting Words-Actions Mismatch",
        "# Critical: Handling Early Function Calls",
        "# Critical: Handling Late Function Calls",
        "Example: If vote_for_session was expected at turn 24 but called at turn 25:",
    ):
        assert expected in _JUDGE_SYSTEM_PROMPT, expected


def test_judge_prompt_has_no_audio_references():
    lowered = _JUDGE_SYSTEM_PROMPT.lower()
    assert "turn_taking" not in lowered
    assert "audio" not in lowered
    assert "THREE dimensions" in _JUDGE_SYSTEM_PROMPT
    assert "FOUR dimensions" not in _JUDGE_SYSTEM_PROMPT


def test_judge_dimensions_are_numbered_one_to_three():
    for n, dim in enumerate(DIMENSIONS, start=1):
        assert f"{n}. **{dim}** (bool):" in _JUDGE_SYSTEM_PROMPT


def test_vendored_upstream_prompt_is_unmodified():
    """The vendored file must stay byte-identical to upstream's literal, so the
    diff above is meaningful. 5802 chars @ aiewf-eval d0dabb6."""
    assert len(_UPSTREAM_JUDGE_PROMPT) == 5802
    assert _UPSTREAM_JUDGE_PROMPT.startswith("# Role\n")
    assert "turn_taking" in _UPSTREAM_JUDGE_PROMPT  # untouched copy


# --- Vendored data files --------------------------------------------------


def test_data_files_ship_with_the_package():
    for name in ("turns.json", "kb_medium.txt", "kb_long.txt",
                 "system_preamble.txt", "system_tools_section.txt"):
        assert (data_loader._DATA / name).is_file(), name


def test_turns_json_is_valid_and_has_no_audio_references():
    raw = json.loads((data_loader._DATA / "turns.json").read_text())
    assert len(raw) == 30
    # audio_file was dropped at vendor time (speech-only).
    assert all("audio_file" not in t for t in raw)
    assert all(set(t) == {"index", "input", "golden_text", "required_function_call"}
               for t in raw)


# --- Jury mode (judge_models) ---------------------------------------------
#
# Single judge stays the default and the upstream-faithful mode; the jury is
# opt-in. These pin the deterministic half: parameter normalization and the
# per-turn, per-dimension majority vote.

from eval_mcp.benchmarks.aiwf.task import _merge_judgments
from eval_mcp.core.jury import normalize_judges


def _verdict(tool=True, instr=True, kb=True, reasoning=""):
    return {
        "tool_use_correct": tool,
        "instruction_following": instr,
        "kb_grounding": kb,
        "reasoning": reasoning,
    }


def test_normalize_judges_defaults_to_the_claude_judge():
    from eval_mcp.core.judge_config import JUDGE_MODELS

    assert normalize_judges(None, None) == [JUDGE_MODELS["claude"]]


def test_normalize_judges_single_judge_passthrough():
    assert normalize_judges("bedrock/j1", None) == ["bedrock/j1"]


def test_normalize_judges_list_wins_over_single():
    assert normalize_judges("bedrock/j1", ["bedrock/a", "bedrock/b"]) == [
        "bedrock/a",
        "bedrock/b",
    ]


def test_normalize_judges_parses_the_comma_scalar_from_the_cli_boundary():
    # -T judge_models=a,b arrives as ONE string; the task splits it back.
    assert normalize_judges(None, "bedrock/a, bedrock/b") == [
        "bedrock/a",
        "bedrock/b",
    ]


def test_normalize_judges_dedupes_preserving_order():
    assert normalize_judges(None, ["b", "a", "b"]) == ["b", "a"]


def test_merge_single_judge_is_the_identity():
    merged, votes = _merge_judgments(
        {"j1": {0: _verdict(kb=False, reasoning="kb wrong")}}
    )
    assert merged[0]["kb_grounding"] is False
    assert merged[0]["tool_use_correct"] is True
    assert merged[0]["reasoning"] == "kb wrong"  # no judge prefix when solo
    assert votes[0]["kb_grounding"] == "0/1"


def test_merge_majority_vote_per_dimension():
    merged, votes = _merge_judgments(
        {
            "j1": {0: _verdict(tool=True, instr=False)},
            "j2": {0: _verdict(tool=True, instr=True)},
            "j3": {0: _verdict(tool=False, instr=False)},
        }
    )
    assert merged[0]["tool_use_correct"] is True  # 2/3
    assert merged[0]["instruction_following"] is False  # 1/3
    assert votes[0]["tool_use_correct"] == "2/3"


def test_merge_tie_fails():
    merged, _ = _merge_judgments(
        {
            "j1": {0: _verdict(kb=True)},
            "j2": {0: _verdict(kb=False)},
        }
    )
    assert merged[0]["kb_grounding"] is False


def test_merge_a_turn_skipped_by_one_judge_uses_the_smaller_denominator():
    # j2 never returned turn 1: it must not count as a False vote there.
    merged, votes = _merge_judgments(
        {
            "j1": {0: _verdict(), 1: _verdict(kb=True)},
            "j2": {0: _verdict()},
        }
    )
    assert merged[1]["kb_grounding"] is True  # 1/1, not 1/2
    assert votes[1]["kb_grounding"] == "1/1"


def test_merge_jury_reasoning_is_attributed_per_judge():
    merged, _ = _merge_judgments(
        {
            "j1": {0: _verdict(reasoning="fine")},
            "j2": {0: _verdict(reasoning="also fine")},
        }
    )
    assert "j1: fine" in merged[0]["reasoning"]
    assert "j2: also fine" in merged[0]["reasoning"]


def test_task_signature_accepts_judge_models():
    import inspect as _inspect

    for factory in (aiwf_medium_context, aiwf_long_context):
        params = _inspect.signature(factory).parameters
        assert "judge_models" in params
        assert params["judge_models"].default is None
