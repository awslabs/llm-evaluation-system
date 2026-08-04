"""AI Engineer World's Fair multi-turn benchmark — Inspect task (text mode).

Two tasks, differing only in knowledge-base size:

    aiwf_medium_context   ~12K-token KB
    aiwf_long_context     ~40K-token KB

Run via ``run_multiturn_benchmark``, or directly:

    inspect eval eval_mcp/benchmarks/aiwf/task.py@aiwf_medium_context \\
        --model bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0

Shape: ONE sample per model = the whole 30-turn conversation. Turns are scripted
(the user side is fixed, so every model faces an identical conversation), and the
model's replies accumulate into the context — which is the point of the
benchmark: performance under a growing multi-turn context, not single-shot Q&A.

Scoring reproduces upstream's three judged dimensions per turn —
``tool_use_correct``, ``instruction_following``, ``kb_grounding`` — and its
headline ``pass_rate = turn_pass / total_turns``, where a turn passes only if
all three hold on that same turn.
"""

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

# Omit max_tokens so Bedrock applies each model's own default rather than
# Inspect's constant 2048. Essential here: a 30-turn conversation with a 40K
# knowledge base is exactly where a reasoning model burns its whole visible
# budget on the reasoning channel and returns empty. See CLAUDE.md
# ("Don't pass max_tokens to evals").
import eval_mcp.inspect_patches  # noqa: F401
from inspect_ai import Task, task
from inspect_ai.dataset import Sample
from inspect_ai.model import (
    ChatMessageSystem,
    ChatMessageUser,
    execute_tools,
    get_model,
)
from inspect_ai.scorer import Metric, SampleScore, Score, Target, metric, scorer
from inspect_ai.solver import Generate, TaskState, solver
from inspect_ai.tool import ToolInfo, ToolParams

from eval_mcp.benchmarks.aiwf.data_loader import (
    AIWF_TASKS,
    system_prompt,
    tool_defs,
    turns,
)
from eval_mcp.core.judge_config import JUDGE_MODELS

# Bump when a change could move scores (upstream's convention, see
# inspect_evals TASK_VERSIONING.md). 1 = initial port of aiewf-eval @ 2c9ae4e.
TASK_VERSION = 1

DIMENSIONS = ("tool_use_correct", "instruction_following", "kb_grounding")


def _max_turns() -> Optional[int]:
    """Turn cap for smoke runs (``EVAL_MCP_AIWF_MAX_TURNS``), else all 30.

    Read from the environment rather than a task parameter so it stays out of
    the benchmark's public signature — a truncated run is a debugging aid, not
    a variant of the benchmark, and its scores are NOT comparable to a full run
    (the tool-call turns cluster in the second half: 11, 12, 15, 17, 24, 29).
    """
    raw = os.environ.get("EVAL_MCP_AIWF_MAX_TURNS")
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


# ---------------------------------------------------------------------------
# Solver: replay the scripted conversation
# ---------------------------------------------------------------------------


# Upstream's synthetic recovery utterance (``MTE_ENABLE_RECOVERY``, default on).
_RECOVERY_UTTERANCE = "Please go ahead."


@solver
def aiwf_conversation(variant: str):
    """Replay all 30 turns against one model, recording per-turn behaviour.

    Two upstream behaviours this reproduces, both of which materially move
    scores — verified against a real run before porting:

    **Turn boundary.** Upstream's text pipeline sets
    ``default_tool_result_run_llm = False``, so a turn ENDS at the tool call.
    The tool result goes into the context for the next turn, but we do not
    generate again within the same turn. Generating twice would hand the model
    a free second attempt and inflate scores.

    **Recovery nudge.** When a turn expected a tool call and the model didn't
    make one, upstream injects ONE synthetic ``"Please go ahead."`` turn and
    merges that attempt into the same scripted turn. This is what lets a model
    that narrates ("Let me submit that for you") before acting still pass. Both
    attempts' tool calls are recorded against the turn, matching upstream's
    ``_has_required_call`` check, which ignores which attempt produced the call.
    Without this the same model scores ~15 points lower than upstream reports.
    """

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        model = get_model()
        state.tools = list(tool_defs())
        state.messages = [ChatMessageSystem(content=system_prompt(variant))]

        cap = _max_turns()
        script = turns()[:cap] if cap else turns()

        records: List[Dict[str, Any]] = []
        recoveries = 0
        for turn in script:
            state.messages.append(ChatMessageUser(content=turn.input))
            output = await model.generate(state.messages, tools=state.tools)
            state.messages.append(output.message)

            calls = list(output.message.tool_calls or [])
            if calls:
                # Tool results must land in context, or the next turn starts
                # from a dangling tool_use block, which Bedrock rejects.
                result = await execute_tools([output.message], state.tools)
                state.messages.extend(result.messages)

            texts = [output.completion or ""]
            recovery_calls: List[Any] = []
            recovered = False
            if turn.expected_tool and not any(
                c.function == turn.expected_tool for c in calls
            ):
                recovered = True
                recoveries += 1
                state.messages.append(ChatMessageUser(content=_RECOVERY_UTTERANCE))
                retry = await model.generate(state.messages, tools=state.tools)
                state.messages.append(retry.message)
                recovery_calls = list(retry.message.tool_calls or [])
                if recovery_calls:
                    retry_result = await execute_tools([retry.message], state.tools)
                    state.messages.extend(retry_result.messages)
                texts.append(retry.completion or "")
                # NOTE: recovery_calls are deliberately NOT merged into `calls`.
                # Upstream records the nudge attempt as a separate transcript
                # entry (recovery_turn=True) whose tool calls sit on that entry —
                # start_turn() resets the recorder's turn_calls first — and its
                # judge skips those entries. So a call that only happened after
                # the nudge is not credited to the scripted turn. The nudge's
                # real effect is on the CONVERSATION: it unblocks the workflow so
                # later turns aren't derailed by the missing call.
                output = retry

            records.append(
                {
                    "turn": turn.index,
                    "user_text": turn.input,
                    # Upstream writes the recovery attempt as a SEPARATE
                    # transcript record flagged recovery_turn=True, and its judge
                    # skips those records entirely
                    # (``if rec.get("recovery_turn"): continue``) — only the
                    # scripted turn is scored. So the judge sees the FIRST
                    # attempt's text, not the nudge reply. We match that: the
                    # recovery attempt's text is kept in
                    # ``recovery_assistant_text`` for debugging but is not shown
                    # to the judge. Concatenating both (an earlier version of
                    # this file) let a model that only complied after the nudge
                    # look like it complied immediately.
                    "assistant_text": texts[0],
                    "recovery_assistant_text": texts[1] if len(texts) > 1 else None,
                    "tool_calls": [
                        {"name": c.function, "args": c.arguments} for c in calls
                    ],
                    # Recorded for debugging + the recoveries_used metric, and
                    # deliberately not shown to the judge (see above).
                    "recovery_tool_calls": [
                        {"name": c.function, "args": c.arguments}
                        for c in recovery_calls
                    ],
                    "expected_function": turn.required_function_call,
                    "golden_text": turn.golden_text,
                    "recovery_used": recovered,
                    # Upstream's TTFT column. Inspect reports total generate
                    # time, not true time-to-first-token (it doesn't stream),
                    # so this is an upper bound — named accordingly.
                    "response_seconds": round(output.time, 3) if output.time else None,
                    "stop_reason": output.stop_reason,
                }
            )
            # Keep the last output as state.output so a truncated final turn is
            # still visible to anything inspecting the sample.
            state.output = output

            if turn.expected_tool == "end_session" and any(
                c.function == "end_session" for c in (calls + recovery_calls)
            ):
                # Upstream terminates the run here. It's the last turn anyway;
                # break rather than relying on that.
                break

        state.metadata["transcript"] = records
        state.metadata["turns_run"] = len(records)
        state.metadata["recoveries_used"] = recoveries
        if cap:
            state.metadata["turn_cap"] = cap
        return state

    return solve


# ---------------------------------------------------------------------------
# Scorer: one judge call over the whole conversation
# ---------------------------------------------------------------------------

# Upstream judges all 30 turns in a SINGLE call, which is what makes its
# early/late tool-call realignment possible: a judge that saw one turn at a
# time couldn't know a function was already called two turns earlier. We keep
# that, so we keep its consequence — the per-turn judgments are not
# independent.
#
# The prompt is upstream's, VERBATIM, loaded from the vendored copy of its
# JUDGE_SYSTEM_PROMPT. Do not paraphrase it: the wording is the measuring
# instrument, and an earlier version of this file rewrote it "more cleanly",
# which silently changed what the benchmark measures. The only edit is the
# mechanical removal of the audio-only `turn_taking` dimension, done by
# transforming upstream's text at load time (rather than hand-editing a copy)
# so the excision stays auditable and can't drift — see _strip_audio_dimension
# and the tests that pin it.


def _strip_audio_dimension(prompt: str) -> str:
    """Remove upstream's audio-only ``turn_taking`` dimension from its prompt.

    We have no audio, so that dimension is undefined. Everything else — the
    two-phase structure, every scoring rule, the worked examples, the output
    schema — is left exactly as upstream wrote it.

    Four mechanical edits:
      1. drop the ``1. **turn_taking**`` block
      2. renumber the remaining dimensions 1..3 and say THREE, not FOUR
      3. drop the "be lenient if turn_taking=FALSE" sub-rule
      4. drop ``turn_taking`` from the output example + its trailing note
    """
    lines = prompt.splitlines()
    out: List[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]

        # 1. the whole "1. **turn_taking** (bool):" block, up to the next
        #    numbered dimension.
        if line.strip().startswith("1. **turn_taking**"):
            i += 1
            while i < len(lines) and not re.match(r"^\d+\. \*\*", lines[i].strip()):
                i += 1
            continue

        # 3. the audio-conditional leniency sub-rule.
        if "If a turn has turn_taking=FALSE" in line:
            i += 1
            continue

        # 4. the trailing note about the turn_taking field.
        if line.startswith("Note: The `turn_taking` field"):
            i += 1
            # also swallow the blank line that followed it
            if i < len(lines) and not lines[i].strip():
                i += 1
            continue

        # 2. dimension count + renumbering (2,3,4 -> 1,2,3).
        if line == "For each turn, evaluate FOUR dimensions:":
            line = "For each turn, evaluate THREE dimensions:"
        else:
            m = re.match(r"^([234])\. \*\*(\w+)\*\* \(bool\):$", line.strip())
            if m:
                line = f"{int(m.group(1)) - 1}. **{m.group(2)}** (bool):"

        # 4. turn_taking key inside the JSON output example.
        if '"turn_taking": true,' in line:
            line = line.replace('"turn_taking": true, ', "")

        out.append(line)
        i += 1
    return "\n".join(out)


_UPSTREAM_JUDGE_PROMPT = (
    Path(__file__).parent / "data" / "upstream_judge_system_prompt.txt"
).read_text(encoding="utf-8")

_JUDGE_SYSTEM_PROMPT = _strip_audio_dimension(_UPSTREAM_JUDGE_PROMPT)


def _render_transcript(records: List[Dict[str, Any]]) -> str:
    """Format the conversation for the judge, following upstream exactly.

    Ports ``format_turns_for_claude``: an "Expected Function Calls Summary"
    header listing every golden call up front, then per-turn User / Assistant /
    Golden Response / Expected Function / Actual Functions. The knowledge base
    is NOT included — upstream's judge doesn't get it either.

    Upstream also emits ``**Turn-Taking**: OK (no audio analysis)`` per turn
    even in text mode. That line is dropped here rather than hard-coded, since
    the dimension it feeds doesn't exist for us.
    """
    lines: List[str] = ["# Expected Function Calls Summary", ""]
    for r in records:
        fc = r.get("expected_function")
        if fc:
            lines.append(f"- Turn {r['turn']}: {fc['name']}({json.dumps(fc['args'])})")
    lines += ["", "---", "", "# Conversation Turns", ""]

    for r in records:
        lines.append(f"## Turn {r['turn']}")
        lines.append(f"**User**: {r['user_text']}")
        assistant = r["assistant_text"] or ""
        if r.get("stop_reason") == "max_tokens":
            # Not upstream's — our runs can hit a token ceiling that its
            # pipeline never surfaced. Marked so the judge doesn't read a
            # severed answer as a factual error.
            assistant += "  [TRUNCATED: hit the model's token ceiling]"
        lines.append(f"**Assistant**: {assistant}")
        lines.append("")
        golden = r.get("golden_text") or ""
        if golden:
            lines.append(f"**Golden Response**: {golden}")
            lines.append("")
        expected = r.get("expected_function")
        lines.append(
            f"**Expected Function**: {json.dumps(expected) if expected else 'none'}"
        )
        actual = r.get("tool_calls") or []
        lines.append(
            f"**Actual Functions**: {json.dumps(actual) if actual else 'none'}"
        )
        lines += ["", "---", ""]
    return "\n".join(lines)


def _render_user_prompt(records: List[Dict[str, Any]]) -> str:
    """Upstream's user-turn prompt: formatted turns + its closing instructions.

    The numbered two-phase instructions and the "Remember:" recap are quoted
    verbatim from upstream's ``judge_with_claude``. An earlier version of this
    port omitted them; they are part of the rubric, not decoration.
    """
    n = len(records)
    return f"""{_render_transcript(records)}

Please perform your two-phase evaluation:
1. First, analyze each turn against its golden expectation
2. Then, identify any turn misalignments (early/late function calls)
3. Apply realignment adjustments to avoid double-penalizing
4. Output the final judgments for ALL {n} turns

CRITICAL: Your judgments array MUST contain exactly {n} entries.

Remember:
- If a function is called early (before expected turn), subsequent turns should not be penalized for the "missing" call
- If a function is called late (after expected turn), penalize the turn that should have called it, credit the turn that did call it, then continue evaluating all remaining turns
- If the assistant says "Does that work?" but doesn't wait for confirmation, that's an instruction_following failure
- If the assistant asks for unnecessary confirmation when it has all needed info, that's a tool_use_correct AND instruction_following failure
- Be generous with kb_grounding unless there's a clear factual error
- Empty assistant_text with a valid tool call is still a valid turn - evaluate the tool call
"""


def _build_judgment_tool(n_turns: int) -> ToolInfo:
    """Forced-output schema: one judgment object per turn.

    Nested arrays-of-objects are riskier than the flat schema the jury scorer
    uses, but per-turn judgments genuinely are structured — and unlike the
    jury, this call has to return 30 of them at once.
    """
    return ToolInfo(
        name="submit_judgments",
        description="Submit one judgment per conversation turn.",
        parameters=ToolParams(
            type="object",
            properties={
                "judgments": {
                    "type": "array",
                    "description": (
                        f"Exactly {n_turns} entries, one per turn, in turn order."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "turn": {
                                "type": "integer",
                                "description": "0-based turn index.",
                            },
                            "tool_use_correct": {"type": "boolean"},
                            "instruction_following": {"type": "boolean"},
                            "kb_grounding": {"type": "boolean"},
                            "reasoning": {
                                "type": "string",
                                "description": "One short sentence. Note any realignment.",
                            },
                        },
                        "required": [
                            "turn",
                            "tool_use_correct",
                            "instruction_following",
                            "kb_grounding",
                        ],
                    },
                }
            },
            required=["judgments"],
        ),
    )


def _extract_judgments(
    output: Any, n_turns: int
) -> tuple[Optional[Dict[int, Dict[str, Any]]], Optional[str]]:
    """Pull the judgments array out of the forced tool call."""
    if not output or not output.message or not output.message.tool_calls:
        text = output.completion[:200] if output and output.completion else "(empty)"
        return None, f"No tool call. Response: {text}"

    raw: List[Any] = []
    for tc in output.message.tool_calls:
        if tc.function == "submit_judgments":
            raw.extend(tc.arguments.get("judgments") or [])
    if not raw:
        return None, "No submit_judgments tool call found"

    by_turn: Dict[int, Dict[str, Any]] = {}
    for j in raw:
        if not isinstance(j, dict) or "turn" not in j:
            continue
        try:
            idx = int(j["turn"])
        except (TypeError, ValueError):
            continue
        by_turn[idx] = {
            **{d: bool(j.get(d, False)) for d in DIMENSIONS},
            "reasoning": str(j.get("reasoning", "") or ""),
        }
    if not by_turn:
        return None, f"No parseable judgments in {len(raw)} entries"
    return by_turn, None


def _split_judges(
    judge_model: Optional[str], judge_models: Optional[Any]
) -> List[str]:
    """Normalize the two judge parameters into an ordered, deduped list.

    ``judge_models`` wins when both are set (it's the superset form) and
    accepts either a real list or a comma-separated string — the latter is how
    it survives the ``-T judge_models=a,b`` CLI boundary, where Inspect parses
    the value as one YAML scalar. Model ids never contain commas.
    """
    models: List[str] = []
    if judge_models:
        raw = (
            judge_models.split(",")
            if isinstance(judge_models, str)
            else list(judge_models)
        )
        models = [str(m).strip() for m in raw if str(m).strip()]
    elif judge_model:
        models = [judge_model]
    if not models:
        models = [JUDGE_MODELS["claude"]]
    seen = set()
    deduped = []
    for m in models:
        if m not in seen:
            seen.add(m)
            deduped.append(m)
    return deduped


def _merge_judgments(
    per_judge: Dict[str, Dict[int, Dict[str, Any]]],
) -> tuple[Dict[int, Dict[str, Any]], Dict[int, Dict[str, str]]]:
    """Majority-vote the per-judge verdicts into one judgment per turn.

    A dimension passes a turn when STRICTLY more than half of the judges who
    returned that turn said True — a tie fails, so an even jury is stricter,
    not random. A turn counts as judged if at least one judge returned it;
    judges that skipped it shrink that turn's denominator rather than voting
    False, mirroring how the single-judge path excludes unjudged turns.

    With one judge this is the identity (1/1 > 1/2), so the single-judge
    fidelity path flows through unchanged. Returns ``(merged, votes)`` where
    votes records the per-dimension tally (``"2/3"``) for the metadata.
    """
    merged: Dict[int, Dict[str, Any]] = {}
    votes: Dict[int, Dict[str, str]] = {}
    solo = len(per_judge) == 1
    for turn in sorted({t for j in per_judge.values() for t in j}):
        verdicts = {m: j[turn] for m, j in per_judge.items() if turn in j}
        n = len(verdicts)
        entry: Dict[str, Any] = {}
        tally: Dict[str, str] = {}
        for d in DIMENSIONS:
            yes = sum(1 for v in verdicts.values() if v[d])
            entry[d] = yes * 2 > n
            tally[d] = f"{yes}/{n}"
        if solo:
            entry["reasoning"] = next(iter(verdicts.values()))["reasoning"]
        else:
            entry["reasoning"] = " | ".join(
                f"{m}: {v['reasoning']}"
                for m, v in verdicts.items()
                if v.get("reasoning")
            )
        merged[turn] = entry
        votes[turn] = tally
    return merged, votes


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _turn_rate(scores: List[SampleScore], key: str) -> float:
    """Aggregate one per-turn counter across samples as a rate over turns.

    Turn-level rather than sample-level: the unit the benchmark reports is the
    turn, while a sample is a whole conversation. Counters are read off score
    metadata so all four rates come from the one judge call.
    """
    num = sum(int(s.score.metadata.get(key, 0) or 0) for s in scores)
    den = sum(int(s.score.metadata.get("turns_judged", 0) or 0) for s in scores)
    return _rate(num, den)


# Each rate needs its own decorated function: @metric fixes the display name at
# decoration time, so a single parameterised factory would report four metrics
# all called "turn_metric" (Inspect disambiguates them as turn_metric2/3/4,
# which is unreadable in the viewer).
@metric("pass_rate")
def pass_rate() -> Metric:
    """Upstream's headline: turns where all 3 dimensions pass, over all turns."""

    def compute(scores: List[SampleScore]) -> float:
        return _turn_rate(scores, "turn_pass")

    return compute


@metric("tool_use_rate")
def tool_use_rate() -> Metric:
    """Turns where the expected tool call was made (or correctly absent)."""

    def compute(scores: List[SampleScore]) -> float:
        return _turn_rate(scores, "tool_use_correct")

    return compute


@metric("instruction_rate")
def instruction_rate() -> Metric:
    """Turns where the assistant answered or advanced the task."""

    def compute(scores: List[SampleScore]) -> float:
        return _turn_rate(scores, "instruction_following")

    return compute


@metric("kb_grounding_rate")
def kb_grounding_rate() -> Metric:
    """Turns free of explicit factual contradictions."""

    def compute(scores: List[SampleScore]) -> float:
        return _turn_rate(scores, "kb_grounding")

    return compute


@metric("truncated_turns")
def truncated_turns() -> Metric:
    """Turns that hit the token ceiling. Non-zero means scores are a floor."""

    def compute(scores: List[SampleScore]) -> float:
        return float(
            sum(int(s.score.metadata.get("truncated_turns", 0) or 0) for s in scores)
        )

    return compute


@scorer(
    metrics=[
        pass_rate(),
        tool_use_rate(),
        instruction_rate(),
        kb_grounding_rate(),
        truncated_turns(),
    ]
)
def aiwf_turn_judge(
    judge_model: Optional[str] = None,
    judge_models: Optional[Any] = None,
):
    """Judge the whole conversation, score per turn.

    Default is a SINGLE judge, matching upstream: one strong judge over the
    full conversation, whose realignment logic depends on seeing every turn
    together. That stays the fidelity mode — its scores are the comparable
    ones.

    ``judge_models`` opts into a jury: each juror independently judges the
    SAME verbatim prompt over the whole transcript (so each can realign), and
    the per-turn, per-dimension verdicts are majority-voted (ties fail). This
    trades upstream comparability for robustness to single-judge noise —
    per-judge agreement lands in the score metadata so the disagreement is
    visible, not averaged away. A judge that errors or returns unparseable
    output is excluded from every vote (recorded in ``judge_errors``), not
    counted as all-False.
    """

    async def score(state: TaskState, target: Target) -> Score:
        records: List[Dict[str, Any]] = state.metadata.get("transcript") or []
        if not records:
            return Score(
                value=0.0,
                explanation="No conversation was recorded — the solver produced nothing.",
                metadata={"turns_judged": 0, "error": "empty_transcript"},
            )

        truncated = sum(1 for r in records if r.get("stop_reason") == "max_tokens")
        jury = _split_judges(judge_model, judge_models)

        messages = [
            ChatMessageSystem(content=_JUDGE_SYSTEM_PROMPT),
            ChatMessageUser(content=_render_user_prompt(records)),
        ]
        tools = [_build_judgment_tool(len(records))]

        async def judge_one(model_id: str) -> tuple[str, Any, Optional[str]]:
            try:
                output = await get_model(model_id).generate(
                    messages, tools=tools, tool_choice="any"
                )
            except Exception as e:
                return model_id, None, f"judge_failed: {e}"
            judgments, err = _extract_judgments(output, len(records))
            if judgments is None:
                return model_id, None, f"unparseable_judge_output: {err}"
            return model_id, judgments, None

        results = await asyncio.gather(*(judge_one(m) for m in jury))
        per_judge = {m: j for m, j, err in results if j is not None}
        judge_errors = {m: err for m, j, err in results if err is not None}

        if not per_judge:  # every judge failed — a measurement failure
            detail = "; ".join(f"{m}: {e}" for m, e in judge_errors.items())
            return Score(
                value=0.0,
                explanation=f"All {len(jury)} judge call(s) failed: {detail}",
                metadata={
                    "turns_judged": 0,
                    "truncated_turns": truncated,
                    "error": f"judge_failed: {detail}",
                    "judge_errors": judge_errors,
                },
            )

        judgments, turn_votes = _merge_judgments(per_judge)

        # Only turns the judge actually returned count toward the denominator.
        # Silently scoring a skipped turn 0 would blame the model for the
        # judge's omission; unjudged_turns surfaces it instead.
        per_turn: List[Dict[str, Any]] = []
        counters = {d: 0 for d in DIMENSIONS}
        turn_pass = 0
        for r in records:
            j = judgments.get(r["turn"])
            if j is None:
                continue
            passed = all(j[d] for d in DIMENSIONS)
            turn_pass += int(passed)
            for d in DIMENSIONS:
                counters[d] += int(j[d])
            entry = {
                "turn": r["turn"],
                **{d: j[d] for d in DIMENSIONS},
                "turn_pass": passed,
                "reasoning": j["reasoning"],
                "recovery_used": bool(r.get("recovery_used")),
                "tools_called": [c["name"] for c in (r.get("tool_calls") or [])],
                "expected_tool": (r.get("expected_function") or {}).get("name"),
                "response_seconds": r.get("response_seconds"),
            }
            if len(per_judge) > 1:
                entry["votes"] = turn_votes.get(r["turn"], {})
            per_turn.append(entry)

        judged = len(per_turn)
        unjudged = len(records) - judged
        pass_rate = _rate(turn_pass, judged)

        latencies = [
            r["response_seconds"] for r in records if r.get("response_seconds")
        ]
        latencies.sort()
        median_seconds = latencies[len(latencies) // 2] if latencies else None

        lines = [
            f"Pass rate: {pass_rate:.3f} ({turn_pass}/{judged} turns passed all "
            f"3 dimensions)",
            "",
            f"  tool_use_correct:      {counters['tool_use_correct']}/{judged}",
            f"  instruction_following: {counters['instruction_following']}/{judged}",
            f"  kb_grounding:          {counters['kb_grounding']}/{judged}",
        ]
        if len(jury) > 1:
            lines += [
                "",
                f"Jury mode: {len(per_judge)} of {len(jury)} judges voted "
                f"({', '.join(per_judge)}); per-dimension majority, ties fail. "
                f"NOT comparable to single-judge (upstream-fidelity) runs.",
            ]
        if judge_errors:
            lines += [
                "",
                f"WARNING: {len(judge_errors)} judge(s) failed and are excluded "
                f"from every vote: "
                + "; ".join(f"{m} ({e[:80]})" for m, e in judge_errors.items()),
            ]
        recoveries = sum(1 for t in per_turn if t["recovery_used"])
        if recoveries:
            lines += [
                "",
                f'Recovery nudges: {recoveries} turn(s) needed a "Please go '
                f'ahead." prompt before the model acted (upstream behaviour; '
                f"the turn can still pass).",
            ]
        if median_seconds is not None:
            lines += [
                "",
                f"Median response time: {median_seconds:.2f}s (total generate "
                f"time, an upper bound on TTFT — Inspect does not stream)",
            ]
        if truncated:
            lines += [
                "",
                f"NOTE: {truncated} turn(s) hit the model's token ceiling "
                f"(stop_reason=max_tokens), so this score is a floor. We pass no "
                f"max_tokens, meaning the ceiling is Bedrock's own default for "
                f"this model — a model/endpoint limitation, not answer quality.",
            ]
        if unjudged:
            lines += [
                "",
                f"WARNING: the judge returned no verdict for {unjudged} of "
                f"{len(records)} turns; those are excluded from the denominator "
                f"rather than scored 0.",
            ]
        cap = state.metadata.get("turn_cap")
        if cap:
            lines += [
                "",
                f"WARNING: this was a capped smoke run ({cap} of {len(turns())} "
                f"turns). NOT comparable to a full run — the tool-call turns "
                f"cluster in the second half of the conversation.",
            ]
        failures = [t for t in per_turn if not t["turn_pass"]]
        if failures:
            lines += ["", "Failed turns:"]
            for t in failures:
                failed_dims = ", ".join(d for d in DIMENSIONS if not t[d])
                lines.append(f"  turn {t['turn']}: {failed_dims} — {t['reasoning']}")

        return Score(
            value=pass_rate,
            answer=f"{turn_pass}/{judged} turns passed",
            explanation="\n".join(lines),
            metadata={
                "turns_judged": judged,
                "turn_pass": turn_pass,
                "unjudged_turns": unjudged,
                "truncated_turns": truncated,
                "median_response_seconds": median_seconds,
                "recoveries_used": recoveries,
                "judge_model": jury[0] if len(jury) == 1 else None,
                "judge_models": jury,
                "jury_mode": len(jury) > 1,
                **({"judge_errors": judge_errors} if judge_errors else {}),
                "task_version": TASK_VERSION,
                "turn_cap": cap,
                **counters,
                "per_turn": per_turn,
            },
        )

    return score


def _aiwf_task(
    variant: str,
    judge_model: Optional[str],
    judge_models: Optional[Any] = None,
) -> Task:
    n_turns = len(turns())
    return Task(
        name=variant,
        dataset=[
            Sample(
                input=(
                    f"{n_turns}-turn AI Engineer World's Fair conversation "
                    f"({variant})"
                ),
                target="see per-turn golden_text",
                metadata={"variant": variant, "turns": n_turns},
            )
        ],
        solver=[aiwf_conversation(variant)],
        scorer=aiwf_turn_judge(judge_model, judge_models),
        version=TASK_VERSION,
    )


@task
def aiwf_medium_context(
    judge_model: Optional[str] = None,
    judge_models: Optional[str] = None,
) -> Task:
    """30-turn conference-assistant conversation, ~12K-token knowledge base."""
    return _aiwf_task("aiwf_medium_context", judge_model, judge_models)


@task
def aiwf_long_context(
    judge_model: Optional[str] = None,
    judge_models: Optional[str] = None,
) -> Task:
    """30-turn conference-assistant conversation, ~40K-token knowledge base."""
    return _aiwf_task("aiwf_long_context", judge_model, judge_models)


# Names exposed by run_multiturn_benchmark. Keys match the @task function names.
AIWF_TASK_NAMES = tuple(AIWF_TASKS)
