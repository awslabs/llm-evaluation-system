"""Multi-judge jury scorer for generated eval configs.

Historically this scorer lived as a template string (``JURY_SCORER_BLOCK``)
inlined into every generated task file by ``create_config.py``. It is now a
real module — imported by generated configs the same way the RAG scorers
(``eval_mcp.scorers.rag``) always were — so it is unit-testable, lintable and
fixable in one place. Task files generated before this change embed the old
inline copy and keep working untouched.

The scorer's behaviour is a measuring instrument; the code was lifted from the
template verbatim. The only changes are mechanical: the ``CONFIG`` module
globals the generated file provided became factory parameters, and template
escaping became plain source. Do not "improve" scoring behaviour here without
re-verifying against a live before/after eval run — pytest cannot prove a
scorer still reads true.

Semantics (unchanged):
- Each judge scores every criterion 0/1 via a forced ``submit_scores`` tool
  call; a judge that errors or returns unparseable output is EXCLUDED from
  every criterion's denominator, not counted as a 0 vote.
- Per-criterion score = fraction of valid judges that passed it; the sample
  score is the mean of those fractions. No thresholds, no majority collapse —
  the fractional form is what the optimizer and reports consume.
- Truncation is distinguished from quality: an empty completion with
  ``stop_reason == "max_tokens"`` is scored 0 with a TRUNCATED explanation
  (``truncated_no_output``), a severed-but-present answer is still scored and
  flagged ``truncated_partial_output``. See CLAUDE.md ("Don't pass max_tokens
  to evals").
"""

from typing import Any, Dict, List, Optional

from inspect_ai.model import ChatMessageSystem, ChatMessageUser, get_model
from inspect_ai.scorer import Score, mean, scorer, stderr
from inspect_ai.tool._tool_info import ToolInfo
from inspect_ai.tool._tool_params import ToolParams

from eval_mcp.core.jury import collect_verdicts, fraction


def _judge_model_args(model_id: str, mantle_regions: Dict[str, str]) -> Dict[str, Any]:
    """Extra get_model() kwargs for a judge — an aws_region override, if needed.

    Region routing for Bedrock Mantle judges is baked in at config-creation
    time by create_eval_config. Mantle model availability is per-region
    (gpt-5.5 and gpt-5.6-sol are us-east-1/us-east-2 only) while AWS
    credentials are global, so a judge that isn't served in the user's own
    region is invoked against one that does serve it. Only openai/bedrock/*
    inference is affected — Converse judges, storage and logs all stay in the
    user's region.
    """
    region = mantle_regions.get(model_id)
    return {"aws_region": region} if region else {}


def _build_scoring_tool(criteria: List[Dict[str, str]]) -> ToolInfo:
    # Schema is intentionally flat: each criterion gets a sibling
    # `<name>_improvement` string slot. Nested objects-per-criterion would
    # be cleaner but Inspect's tool-forced output handles flat int/string
    # fields most reliably across models. Improvement slots are optional —
    # old judge runs without them still parse fine.
    properties = {}
    required = []
    for c in criteria:
        properties[c["name"]] = {
            "type": "integer",
            "description": f"Score for {c['name']}: 1 if pass, 0 if fail",
            "enum": [0, 1],
        }
        required.append(c["name"])
        properties[f"{c['name']}_improvement"] = {
            "type": "string",
            "description": (
                f"If {c['name']} scored 0, ONE short sentence on what the "
                "answer should change to satisfy the criterion. Empty string "
                "when scored 1."
            ),
        }
    properties["reason"] = {
        "type": "string",
        "description": "Brief overall explanation of the scoring decision",
    }
    required.append("reason")

    return ToolInfo(
        name="submit_scores",
        description="Submit binary scores plus per-criterion improvement hints",
        parameters=ToolParams(type="object", properties=properties, required=required),
    )


def _extract_scores(output: Any, criteria_names: List[str]):
    """Pull scores + per-criterion improvement notes + shared reason out
    of the judge's submit_scores call. Returns
    ``(scores, reason, improvements, error)`` where improvements maps
    criterion name -> string (empty when the judge passed the criterion
    or omitted the hint).
    """
    if not output or not output.message or not output.message.tool_calls:
        text = output.completion[:200] if output and output.completion else "(empty)"
        return None, None, None, f"No tool call. Response: {text}"

    args = {}
    for tc in output.message.tool_calls:
        if tc.function == "submit_scores":
            args.update(tc.arguments)

    if not args:
        return None, None, None, "No submit_scores tool call found"

    missing = [n for n in criteria_names if n not in args]
    if missing:
        return None, None, None, f"Missing criteria: {missing}. Got: {list(args.keys())}"

    scores = {n: int(bool(args[n])) for n in criteria_names}
    improvements = {
        n: str(args.get(f"{n}_improvement", "") or "").strip()
        for n in criteria_names
    }
    return scores, args.get("reason", ""), improvements, None


@scorer(metrics=[mean(), stderr()])
def jury_scorer(
    criteria: List[Dict[str, str]],
    judge_models: Dict[str, str],
    system_prompt: str,
    mantle_regions: Optional[Dict[str, str]] = None,
):
    """Score one sample with a panel of judges, averaging per-criterion votes.

    Args mirror the generated config's fields — the generated task file calls
    ``jury_scorer(CONFIG["criteria"], CONFIG["judge_models"],
    CONFIG["system_prompt"], CONFIG.get("mantle_regions"))``.
    """
    mantle_regions = mantle_regions or {}

    async def score(state, target):
        output = state.output.completion if state.output else ""
        if not output:
            # Distinguish "the model produced a bad answer" from "the model
            # never got to answer". A reasoning model (gpt-5.6-*, gpt-oss-*)
            # can spend its entire token budget on the reasoning channel and
            # emit zero visible tokens: stop_reason == "max_tokens" with an
            # empty completion. Scoring that a plain 0 is a measurement error
            # masquerading as a quality signal — it silently penalises exactly
            # the models that think hardest, and the run still reports success.
            stop_reason = getattr(state.output, "stop_reason", None) if state.output else None
            if stop_reason == "max_tokens":
                return Score(
                    value=0.0,
                    answer="",
                    explanation=(
                        "TRUNCATED: the model hit its max_tokens limit before emitting "
                        "any answer (all output tokens went to the reasoning channel). "
                        "This is a token-budget problem, NOT a quality result — do not "
                        "compare this model against others on this run. We pass no "
                        "max_tokens, so the ceiling is Bedrock's own default for this "
                        "model, which for some reasoning models is smaller than the task "
                        "needs. This is a known limitation of the model/endpoint, not a "
                        "measure of answer quality."
                    ),
                    metadata={"truncated_no_output": True, "stop_reason": stop_reason},
                )
            return Score(value=0.0, answer="", explanation="No output generated")

        # Output exists but was cut off mid-answer. Unlike the empty case above
        # this still gets scored — a partial answer carries real signal, and
        # discarding it would throw away every long response. But the score is
        # depressed by the truncation, not only by quality (criteria like
        # completeness necessarily fail on a severed answer), so flag it: a
        # reader comparing models needs to know this number is a floor, not a
        # measurement. Recorded in metadata rather than the score so it can't
        # silently pass as a clean result.
        truncated_partial = (
            getattr(state.output, "stop_reason", None) == "max_tokens"
            if state.output
            else False
        )

        question = str(state.input)
        golden = target.text if target else ""
        criteria_names = [c["name"] for c in criteria]
        tool = _build_scoring_tool(criteria)

        async def call(label: str, model_id: str):
            judge = get_model(model_id, **_judge_model_args(model_id, mantle_regions))
            result = await judge.generate(
                [
                    ChatMessageSystem(content=system_prompt),
                    ChatMessageUser(
                        content=f"Question:\n{question}\n\nAI Answer:\n{output}\n\nReference Answer:\n{golden}"
                    ),
                ],
                tools=[tool],
                tool_choice="any",
            )
            scores, reason, improvements, err = _extract_scores(result, criteria_names)
            if scores is None:
                return None, err
            return (scores, reason, improvements), None

        # Sequential fan-out (parallel=False), as this scorer always was —
        # a burst of parallel judge calls trips Bedrock throttling on large
        # runs, and a throttled judge becomes an exclusion, which moves scores.
        verdicts, judge_errors = await collect_verdicts(
            judge_models, call, parallel=False
        )

        votes = {n: [] for n in criteria_names}
        # Per-criterion improvement hints collected from judges that
        # scored 0. List of {judge, note} pairs so downstream
        # consumers (optimizer, report) can attribute hints to judges
        # and de-dupe across them.
        improvements_per_criterion = {n: [] for n in criteria_names}
        details = []
        errors = []

        for label in judge_models:
            if label in verdicts:
                scores, reason, improvements = verdicts[label]
                for n in criteria_names:
                    votes[n].append(scores[n])
                    if scores[n] == 0 and improvements and improvements.get(n):
                        improvements_per_criterion[n].append(
                            {"judge": label, "note": improvements[n]}
                        )
                details.append(f"  {label}: {scores} - {reason}")
            elif label in judge_errors:
                kind, err = judge_errors[label]
                errors.append(f"  {label}: {err[:200]}")
                # Same wording the inline original used: EXCLUDED for a judge
                # that answered unparseably, ERROR for a call that raised.
                word = "EXCLUDED" if kind == "unparseable" else "ERROR"
                details.append(f"  {label}: {word} ({err[:80]})")

        results = []
        for n in criteria_names:
            v = votes[n]
            if not v:
                results.append({"name": n, "votes_for": 0, "total": 0, "score": 0.0, "note": "no valid responses"})
            else:
                entry = {"name": n, "votes_for": sum(v), "total": len(v), "score": fraction(v)}
                if improvements_per_criterion[n]:
                    entry["improvement_notes"] = improvements_per_criterion[n]
                results.append(entry)

        # Sample score = mean of per-criterion judge-fractions. No thresholds.
        scored = [r for r in results if "note" not in r]
        jury_score = sum(r["score"] for r in scored) / len(scored) if scored else 0.0

        lines = [f"Jury score: {jury_score:.2f} ({len(scored)}/{len(criteria_names)} criteria graded)", ""]
        for r in results:
            extra = f" - {r['note']}" if "note" in r else ""
            lines.append(f"  {r['name']}: {r['score']:.2f} ({r['votes_for']}/{r['total']} judges){extra}")
        lines += ["", "Judges:"] + details
        if errors:
            lines += ["", "Errors:"] + errors

        if truncated_partial:
            lines.append(
                "NOTE: the answer was cut off at the token ceiling "
                "(stop_reason=max_tokens), so this score is a floor — criteria "
                "like completeness fail on a severed answer regardless of quality."
            )

        return Score(
            value=jury_score,
            answer=output[:200],
            explanation="\n".join(lines),
            metadata={
                "jury_score": jury_score,
                "criteria_results": results,
                "truncated_partial_output": truncated_partial,
            },
        )

    return score
