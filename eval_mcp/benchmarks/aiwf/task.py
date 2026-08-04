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

import json
import os
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
            recovered = False
            if turn.expected_tool and not any(
                c.function == turn.expected_tool for c in calls
            ):
                recovered = True
                recoveries += 1
                state.messages.append(ChatMessageUser(content=_RECOVERY_UTTERANCE))
                retry = await model.generate(state.messages, tools=state.tools)
                state.messages.append(retry.message)
                retry_calls = list(retry.message.tool_calls or [])
                if retry_calls:
                    retry_result = await execute_tools([retry.message], state.tools)
                    state.messages.extend(retry_result.messages)
                calls.extend(retry_calls)
                texts.append(retry.completion or "")
                output = retry

            records.append(
                {
                    "turn": turn.index,
                    "user_text": turn.input,
                    # Both attempts' text, so the judge sees what the model
                    # actually said across the turn.
                    "assistant_text": "\n".join(t for t in texts if t),
                    "tool_calls": [
                        {"name": c.function, "args": c.arguments} for c in calls
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
                c.function == "end_session" for c in calls
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
_JUDGE_SYSTEM_PROMPT = """You are an expert evaluator for conversational AI \
systems. You will judge a multi-turn conversation between a user and an AI \
assistant for the AI Engineer World's Fair 2025.

# CRITICAL: Evaluate ALL turns

You MUST output a judgment for EVERY turn provided in the input. Do not stop \
early or skip turns. Even if the conversation went off-track, continue \
evaluating all remaining turns.

# Evaluation dimensions

For each turn, evaluate THREE dimensions:

1. **tool_use_correct** (bool):
   - TRUE if the assistant called the expected function with semantically \
equivalent arguments
   - TRUE if no function call was expected and none was made
   - TRUE if a function call was expected but was already made in an earlier \
turn (realignment case)
   - TRUE if a late function call is made at this turn (the call eventually \
happened, credit this turn)
   - FALSE if a function call was expected, not made, and NOT already made \
earlier
   - FALSE if the assistant's words imply waiting for confirmation but it acts \
without waiting
   - FALSE if the assistant asks for unnecessary confirmation instead of \
making the expected function call
   - For argument matching, use semantic equivalence (not verbatim). Session \
IDs must match exactly.

2. **instruction_following** (bool):
   - TRUE if the assistant directly answers the question OR advances the task
   - TRUE if the assistant properly deflects out-of-scope questions
   - TRUE if the turn is part of a realigned workflow that still accomplishes \
the goal
   - FALSE if the assistant's words contradict its actions (says "Does that \
work?" but doesn't wait)
   - FALSE if the assistant neither answers nor advances the workflow
   - FALSE if the assistant asks for unnecessary confirmation when it already \
has all needed information

3. **kb_grounding** (bool):
   - TRUE unless the assistant states an explicit factual error
   - TRUE if the assistant provides additional correct information
   - FALSE only for clear factual contradictions (wrong dates, times, \
locations, speakers)
   - Judge against the Golden Response shown for each turn. You do NOT have \
the knowledge base; do not penalise detail you cannot verify.

# Handling early function calls

When you detect an early function call: note which function and at which turn. \
In subsequent turns, if that same function was "expected", mark \
tool_use_correct TRUE (already satisfied) and explain the realignment.

# Handling late function calls

When the assistant asked for unnecessary confirmation instead of acting: \
penalise the turn where the function SHOULD have been called \
(tool_use_correct=FALSE, instruction_following=FALSE), credit the turn where \
it WAS called (tool_use_correct=TRUE), and keep evaluating all later turns.

# Empty assistant text with a tool call

A turn with empty assistant text but a valid tool call is still valid. The \
assistant may have called the function without speaking. Evaluate the tool \
call normally.

# Truncated turns

A turn marked TRUNCATED hit the model's token ceiling before finishing. Judge \
what is present; do not treat the truncation itself as a factual error.

Submit your judgments with the submit_judgments tool. Provide exactly one \
entry per turn, in order."""


def _render_transcript(records: List[Dict[str, Any]]) -> str:
    """Format the conversation for the judge.

    Mirrors upstream's ``build_judge_prompt``: user text, assistant text,
    golden response, expected function, actual functions. The knowledge base is
    deliberately NOT included — upstream's judge doesn't get it either, and
    including it would change what kb_grounding measures (and add ~92K tokens
    per judge call on the long variant).
    """
    lines: List[str] = []
    for r in records:
        lines.append(f"## Turn {r['turn']}")
        lines.append(f"**User**: {r['user_text']}")
        assistant = r["assistant_text"] or "(no text)"
        if r.get("stop_reason") == "max_tokens":
            assistant += "  [TRUNCATED: hit token ceiling]"
        lines.append(f"**Assistant**: {assistant}")
        if r.get("recovery_used"):
            # Upstream merges the nudge attempt into the same turn, so the judge
            # must know both replies belong to one turn — otherwise it reads the
            # concatenation as the assistant rambling or repeating itself.
            lines.append(
                "**Note**: the assistant did not act on the first attempt, so a "
                'synthetic "Please go ahead." nudge was sent; the text above '
                "combines both replies of this one turn."
            )
        lines.append("")
        if r.get("golden_text"):
            lines.append(f"**Golden Response**: {r['golden_text']}")
            lines.append("")
        expected = r.get("expected_function")
        lines.append(
            f"**Expected Function**: {json.dumps(expected) if expected else 'none'}"
        )
        actual = r.get("tool_calls") or []
        lines.append(
            f"**Actual Functions**: {json.dumps(actual) if actual else 'none'}"
        )
        lines.append("")
        lines.append("---")
        lines.append("")
    return "\n".join(lines)


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
def aiwf_turn_judge(judge_model: Optional[str] = None):
    """Judge the whole conversation in one call, score per turn.

    Single judge, not our usual jury: upstream uses one strong judge over the
    full conversation, and the realignment logic depends on seeing every turn
    together. Three jurors would each need the whole 30-turn transcript, and
    majority-voting per turn would still not make the turns independent.
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
        model_id = judge_model or JUDGE_MODELS["claude"]

        try:
            judge = get_model(model_id)
            output = await judge.generate(
                [
                    ChatMessageSystem(content=_JUDGE_SYSTEM_PROMPT),
                    ChatMessageUser(content=_render_transcript(records)),
                ],
                tools=[_build_judgment_tool(len(records))],
                tool_choice="any",
            )
        except Exception as e:  # judge failure is a measurement failure
            return Score(
                value=0.0,
                explanation=f"Judge call failed: {e}",
                metadata={
                    "turns_judged": 0,
                    "truncated_turns": truncated,
                    "error": f"judge_failed: {e}",
                },
            )

        judgments, err = _extract_judgments(output, len(records))
        if judgments is None:
            return Score(
                value=0.0,
                explanation=f"Could not parse judge output: {err}",
                metadata={
                    "turns_judged": 0,
                    "truncated_turns": truncated,
                    "error": f"unparseable_judge_output: {err}",
                },
            )

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
            per_turn.append(
                {
                    "turn": r["turn"],
                    **{d: j[d] for d in DIMENSIONS},
                    "turn_pass": passed,
                    "reasoning": j["reasoning"],
                    "recovery_used": bool(r.get("recovery_used")),
                    "tools_called": [c["name"] for c in (r.get("tool_calls") or [])],
                    "expected_tool": (r.get("expected_function") or {}).get("name"),
                    "response_seconds": r.get("response_seconds"),
                }
            )

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
                "judge_model": model_id,
                "task_version": TASK_VERSION,
                "turn_cap": cap,
                **counters,
                "per_turn": per_turn,
            },
        )

    return score


def _aiwf_task(variant: str, judge_model: Optional[str]) -> Task:
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
        scorer=aiwf_turn_judge(judge_model),
        version=TASK_VERSION,
    )


@task
def aiwf_medium_context(judge_model: Optional[str] = None) -> Task:
    """30-turn conference-assistant conversation, ~12K-token knowledge base."""
    return _aiwf_task("aiwf_medium_context", judge_model)


@task
def aiwf_long_context(judge_model: Optional[str] = None) -> Task:
    """30-turn conference-assistant conversation, ~40K-token knowledge base."""
    return _aiwf_task("aiwf_long_context", judge_model)


# Names exposed by run_multiturn_benchmark. Keys match the @task function names.
AIWF_TASK_NAMES = tuple(AIWF_TASKS)
