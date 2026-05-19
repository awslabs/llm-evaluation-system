"""Generate custom evaluation criteria for Jury multi-judge evaluation.

Two-stage pipeline:
  1. ``generate_judge`` — one LLM call analyses the dataset and proposes
     binary criteria (wide net, redundancy allowed).
  2. ``refine_criteria_loop`` — up to ``CRITIC_MAX_ITERATIONS`` passes
     score a sample of QA pairs against the current criteria, then ask a
     critic LLM to drop redundant criteria, rewrite vague ones, and add
     anything missing. Exits early once the critic returns
     ``no_changes_needed``.

The asymmetry is intentional: the eval-time **jury**
(``backend/core/jury_scoring.py``) uses 3 judges to smooth measurement
noise. The design-time **critic** is a single judge call because it's a
craft judgment, and the user reviews the final criteria anyway.
"""

import asyncio
import json
import logging
import os
import random
from typing import Any, Dict, List, Optional

from mcp.types import TextContent

from eval_mcp.core.bedrock_client import BedrockClient
from eval_mcp.core.user_storage import save_judge_to_db, get_dataset_by_name
from eval_mcp.core.judge_config import MAX_CRITERIA, DEFAULT_CRITERIA

logger = logging.getLogger(__name__)

# How many critic refinement passes after the initial generation. Each
# pass costs ~CRITIC_SAMPLE_SIZE judge calls + one critic call. Most
# domains converge by iteration 2; 3 leaves headroom without burning
# tokens on diminishing returns.
CRITIC_MAX_ITERATIONS = int(os.environ.get("EVAL_MCP_CRITIC_MAX_ITERATIONS", "3"))

# QA pairs scored per refinement pass to give the critic enough signal
# to spot per-criterion patterns (always-passes, redundancy with another
# criterion) without ballooning iteration cost.
CRITIC_SAMPLE_SIZE = 10

# Tool schema for structured criteria output
CRITERIA_TOOL = {
    "name": "submit_criteria",
    "description": "Submit the evaluation criteria you've designed.",
    "input_schema": {
        "type": "object",
        "properties": {
            "criteria": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Short snake_case name for the criterion (e.g., factual_accuracy)"
                        },
                        "description": {
                            "type": "string",
                            "description": "Binary description: '1 if [condition], 0 otherwise'"
                        }
                    },
                    "required": ["name", "description"]
                },
                "minItems": 3,
                "maxItems": 15,
                "description": "Evaluation criteria for judging responses. Aim for coverage of what distinguishes good answers from bad — the critic loop will drop redundant ones afterwards."
            }
        },
        "required": ["criteria"]
    }
}


async def generate_judge(
    bedrock: BedrockClient,
    qa_pairs: List[Dict[str, str]],
    domain: str,
) -> Dict[str, Any]:
    """Generate custom evaluation criteria based on QA pairs.

    Analyzes up to 10 QA pairs to create domain-specific binary criteria
    for Jury multi-judge evaluation.

    Args:
        bedrock: Bedrock client
        qa_pairs: List of {"question": "...", "golden_answer": "..."} dicts
        domain: Domain/purpose of the evaluation

    Returns:
        Dict with "criteria" list for JudgeConfig
    """
    # Format default criteria as examples
    default_examples = "\n".join([
        f"  - {c['name']}: {c['description']}" for c in DEFAULT_CRITERIA
    ])

    system_prompt = f"""You are an expert at designing evaluation criteria for LLM outputs.
Your task is to analyze question-answer pairs and create binary (0 or 1) evaluation criteria.

Guidelines:
- Create criteria that capture what matters for this domain — favor coverage over brevity, a downstream critic will prune redundancies
- Each criterion must be binary: "1 if [specific condition], 0 otherwise"
- Focus on what actually matters based on the QA examples
- Use snake_case for criterion names (e.g., factual_accuracy, completeness)
- Make criteria objective and clear for multiple judges to apply consistently
- Responses will be compared against a reference (golden) answer — criteria should reflect closeness to the reference

Here are example criteria to use as inspiration (adapt to the domain, don't copy verbatim):
{default_examples}"""

    # Format QA pairs for analysis (up to 10)
    qa_examples = "\n\n".join([
        f"Q{i+1}: {qa['question']}\nA{i+1}: {qa['golden_answer']}"
        for i, qa in enumerate(qa_pairs[:10])
    ])

    user_prompt = f"""Analyze these question-answer pairs from a {domain} evaluation:

<QA_Examples>
{qa_examples}
</QA_Examples>

Based on these examples, design binary evaluation criteria that capture what makes a good response in this domain. Cast a wide net — a downstream critic will trim redundancies and rewrite vague ones.

Consider:
- What factual elements must be correct?
- What level of completeness is expected?
- Are there format/structure requirements evident in the golden answers?
- What domain-specific qualities matter?

Call the submit_criteria tool with your criteria."""

    messages = [{"role": "user", "content": user_prompt}]

    response = await asyncio.to_thread(
        bedrock.create_message,
        messages=messages,
        system=system_prompt,
        tools=[CRITERIA_TOOL],
        tool_choice={"type": "auto"},
        max_tokens=2048,
        temperature=0,  # Deterministic output for reproducible criteria
    )

    # Extract tool use result
    for block in response.get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == "submit_criteria":
            return block.get("input", {})

    # Fallback if no tool use found
    raise ValueError("Failed to generate structured criteria")


# ---------------------------------------------------------------------------
# Critic-led refinement loop
# ---------------------------------------------------------------------------


# Forced-tool schema for scoring a single (question, golden) against
# the current criteria. Inline rather than imported from
# backend/core/judge_tools.py because eval_mcp/ should not depend on
# backend/, and the schema is small enough that duplication is cheaper
# than introducing a new shared module.
def _build_scoring_tool(criteria: List[Dict[str, str]]) -> Dict[str, Any]:
    properties: Dict[str, Any] = {}
    required: List[str] = []
    for c in criteria:
        properties[c["name"]] = {
            "type": "object",
            "properties": {
                "score": {
                    "type": "integer",
                    "enum": [0, 1],
                    "description": c["description"],
                },
                "reason": {
                    "type": "string",
                    "description": "One short sentence — why this score.",
                },
            },
            "required": ["score", "reason"],
        }
        required.append(c["name"])
    return {
        "name": "submit_scores",
        "description": "Score each criterion 0 or 1 against the reference answer with a brief reason.",
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": required,
        },
    }


# Forced-tool schema for the critic. It can drop / keep / rewrite
# existing criteria and propose net-new ones. ``no_changes_needed`` is
# the loop's natural exit signal.
CRITIC_TOOL: Dict[str, Any] = {
    "name": "submit_critique",
    "description": "Submit your critique of the current criteria set.",
    "input_schema": {
        "type": "object",
        "properties": {
            "no_changes_needed": {
                "type": "boolean",
                "description": "True if the criteria set is good and the loop should exit.",
            },
            "criteria_updates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "The existing criterion name this update applies to.",
                        },
                        "action": {
                            "type": "string",
                            "enum": ["keep", "drop", "rewrite"],
                        },
                        "new_name": {
                            "type": "string",
                            "description": "Required when action is 'rewrite' — the replacement snake_case name.",
                        },
                        "new_description": {
                            "type": "string",
                            "description": "Required when action is 'rewrite' — binary '1 if X, 0 otherwise' form.",
                        },
                        "reason": {
                            "type": "string",
                            "description": "One short sentence — why this change.",
                        },
                    },
                    "required": ["name", "action", "reason"],
                },
                "description": "Per-criterion actions. Omit a criterion to leave it unchanged.",
            },
            "new_criteria": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["name", "description"],
                },
                "description": "Brand-new criteria to add to the set.",
            },
        },
        "required": ["no_changes_needed"],
    },
}


async def _score_qa_pair(
    bedrock: BedrockClient,
    criteria: List[Dict[str, str]],
    qa_pair: Dict[str, str],
) -> Dict[str, Dict[str, Any]]:
    """Score one QA pair against the criteria. Returns
    ``{criterion_name: {"score": 0|1, "reason": "..."}}``.

    Forces the ``submit_scores`` tool so the response is always valid
    JSON matching the schema — no text-parsing failure modes.
    """
    scoring_tool = _build_scoring_tool(criteria)
    criteria_lines = "\n".join(
        f"- {c['name']}: {c['description']}" for c in criteria
    )
    user_prompt = (
        "Score the reference answer against each criterion. You are NOT "
        "comparing two answers — you're checking whether the reference "
        "satisfies each criterion definition.\n\n"
        f"Criteria:\n{criteria_lines}\n\n"
        f"[Question]\n{qa_pair['question']}\n\n"
        f"[Reference Answer]\n{qa_pair['golden_answer']}\n\n"
        "Call submit_scores with your assessment."
    )
    response = await asyncio.to_thread(
        bedrock.create_message,
        messages=[{"role": "user", "content": user_prompt}],
        tools=[scoring_tool],
        tool_choice={"type": "tool", "name": "submit_scores"},
        max_tokens=2048,
        temperature=0,
    )
    for block in response.get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == "submit_scores":
            return block.get("input", {})
    raise ValueError("Judge call returned no submit_scores tool_use")


async def _score_samples(
    bedrock: BedrockClient,
    criteria: List[Dict[str, str]],
    samples: List[Dict[str, str]],
) -> List[Dict[str, Any]]:
    """Score every sample in parallel and return scored rows shaped for
    the critic prompt: ``{"question", "golden", "scores"}``.

    Failures on individual samples don't abort — they're dropped from
    the batch so the critic gets whatever signal we managed to collect.
    Catastrophic empty result is caller's problem.
    """
    tasks = [_score_qa_pair(bedrock, criteria, s) for s in samples]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    scored: List[Dict[str, Any]] = []
    for sample, result in zip(samples, results):
        if isinstance(result, Exception):
            logger.warning("Skipping sample during critic scoring: %s", result)
            continue
        scored.append(
            {
                "question": sample["question"],
                "golden": sample["golden_answer"],
                "scores": result,
            }
        )
    return scored


def _format_scored_for_critic(scored: List[Dict[str, Any]]) -> str:
    """Render scored samples as a compact text block for the critic."""
    lines: List[str] = []
    for i, row in enumerate(scored, start=1):
        lines.append(f"Sample {i}:")
        lines.append(f"  Q: {row['question'][:200]}")
        lines.append(f"  Golden: {row['golden'][:300]}")
        lines.append("  Scores:")
        for name, payload in row["scores"].items():
            score = payload.get("score") if isinstance(payload, dict) else payload
            reason = payload.get("reason", "") if isinstance(payload, dict) else ""
            lines.append(f"    - {name}: {score} ({reason})")
        lines.append("")
    return "\n".join(lines)


def _criterion_pass_rates(
    scored: List[Dict[str, Any]],
    criteria: List[Dict[str, str]],
) -> Dict[str, str]:
    """Per-criterion pass rate summary, e.g. ``{"factual_accuracy": "5/5"}``.

    Surfaced separately to the critic because the aggregate is what
    reveals "always passes" / "always fails" — patterns that are easy
    to miss when scrolling through per-sample scores.
    """
    out: Dict[str, str] = {}
    for c in criteria:
        name = c["name"]
        total = 0
        passes = 0
        for row in scored:
            payload = row["scores"].get(name)
            if payload is None:
                continue
            total += 1
            score = payload.get("score") if isinstance(payload, dict) else payload
            if score == 1:
                passes += 1
        out[name] = f"{passes}/{total}" if total else "0/0"
    return out


_CRITIC_SYSTEM_PROMPT = (
    "You are critiquing a set of binary (0/1) evaluation criteria used to "
    "judge LLM answers against golden answers. You see how each criterion "
    "scored across a handful of golden answers plus the judge's reasons.\n\n"
    "Your job is to make the criteria set better:\n"
    "- DROP criteria that duplicate another criterion (measuring the same thing).\n"
    "- DROP or REWRITE criteria that always pass or always fail on the "
    "samples — they carry no signal as written.\n"
    "- REWRITE criteria that are vague or whose 'reason' lines show the "
    "judge guessing — tighten the definition.\n"
    "- ADD missing criteria for failure modes the current set wouldn't catch.\n\n"
    "If the set is already good, set no_changes_needed=true and skip the "
    "rest. Otherwise call submit_critique with your changes. Per-criterion "
    "actions default to 'keep' if you omit them — only list the ones you "
    "want to drop or rewrite."
)


async def _critique_criteria(
    bedrock: BedrockClient,
    criteria: List[Dict[str, str]],
    scored: List[Dict[str, Any]],
    domain: str,
) -> Dict[str, Any]:
    """Run one critic pass. Returns the parsed ``submit_critique`` payload.

    Raises ``ValueError`` if the model failed to call the tool — caller
    should treat that as an iteration failure and bail to last-good.
    """
    criteria_block = "\n".join(
        f"- {c['name']}: {c['description']}" for c in criteria
    )
    pass_rates = _criterion_pass_rates(scored, criteria)
    pass_rate_block = "\n".join(
        f"  - {name}: {rate} samples passed" for name, rate in pass_rates.items()
    )
    user_prompt = (
        f"Domain: {domain}\n\n"
        f"Current criteria ({len(criteria)}):\n{criteria_block}\n\n"
        f"Per-criterion pass rate across {len(scored)} sampled golden answers:\n"
        f"{pass_rate_block}\n\n"
        f"Per-sample scoring detail:\n{_format_scored_for_critic(scored)}\n"
        "Call submit_critique with your decision."
    )
    response = await asyncio.to_thread(
        bedrock.create_message,
        messages=[{"role": "user", "content": user_prompt}],
        system=_CRITIC_SYSTEM_PROMPT,
        tools=[CRITIC_TOOL],
        tool_choice={"type": "tool", "name": "submit_critique"},
        max_tokens=3000,
        temperature=0,
    )
    for block in response.get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == "submit_critique":
            return block.get("input", {})
    raise ValueError("Critic call returned no submit_critique tool_use")


def _apply_updates(
    criteria: List[Dict[str, str]],
    critique: Dict[str, Any],
) -> List[Dict[str, str]]:
    """Walk ``criteria`` and apply the critique's drop/rewrite/new actions.

    Pure function — no Bedrock calls. Returns a new list; does not
    mutate the input. Caller is responsible for capping at
    ``MAX_CRITERIA`` if the additions push over.

    Update semantics:
      - Action ``drop``: criterion removed from output.
      - Action ``rewrite``: criterion replaced in place (preserves
        order) using ``new_name`` / ``new_description`` if provided,
        else left unchanged with a warning.
      - Action ``keep`` or missing entry: criterion kept as-is.
      - Anything in ``new_criteria`` appended to the end with name +
        description fields only (other keys discarded).
    """
    updates_by_name: Dict[str, Dict[str, Any]] = {
        u.get("name"): u for u in critique.get("criteria_updates", []) if u.get("name")
    }

    result: List[Dict[str, str]] = []
    for c in criteria:
        update = updates_by_name.get(c["name"])
        if update is None or update.get("action") == "keep":
            result.append({"name": c["name"], "description": c["description"]})
            continue
        action = update.get("action")
        if action == "drop":
            continue
        if action == "rewrite":
            new_name = update.get("new_name") or c["name"]
            new_desc = update.get("new_description") or c["description"]
            result.append({"name": new_name, "description": new_desc})
            continue
        # Unknown action — keep, don't lose data.
        logger.warning("Unknown critic action %r for criterion %r; keeping unchanged.", action, c["name"])
        result.append({"name": c["name"], "description": c["description"]})

    for new in critique.get("new_criteria", []):
        name = new.get("name")
        desc = new.get("description")
        if name and desc:
            result.append({"name": name, "description": desc})

    return result


async def refine_criteria_loop(
    bedrock: BedrockClient,
    criteria: List[Dict[str, str]],
    qa_pairs: List[Dict[str, str]],
    domain: str,
    max_iter: int = CRITIC_MAX_ITERATIONS,
    sample_size: int = CRITIC_SAMPLE_SIZE,
) -> List[Dict[str, str]]:
    """Iteratively refine criteria using critic feedback on scored samples.

    Exits early when the critic returns ``no_changes_needed``. On any
    exception inside an iteration (Bedrock outage, malformed tool
    output, etc.), returns the last-good criteria so a flaky round
    can't wipe out a usable set.

    Args:
        bedrock: Bedrock client singleton (already pinned to the user's
            chosen Claude model).
        criteria: Starting criteria from the initial generation pass.
        qa_pairs: Full dataset of ``{question, golden_answer}`` rows;
            sampled per iteration.
        domain: Domain string from the user — passed to the critic for
            context.
        max_iter: Hard ceiling on iterations. Reaching it exits silently.
        sample_size: QA pairs scored per iteration.

    Returns:
        Refined criteria list, capped at ``MAX_CRITERIA``.
    """
    if not criteria or not qa_pairs or max_iter <= 0:
        return criteria

    last_good = criteria
    for i in range(1, max_iter + 1):
        try:
            sample_n = min(sample_size, len(qa_pairs))
            samples = random.sample(qa_pairs, sample_n) if sample_n < len(qa_pairs) else list(qa_pairs)

            scored = await _score_samples(bedrock, last_good, samples)
            if not scored:
                logger.warning("Critic iter %d: all scoring calls failed; stopping with last-good criteria.", i)
                return last_good

            critique = await _critique_criteria(bedrock, last_good, scored, domain)
            if critique.get("no_changes_needed"):
                logger.info("Critic iter %d: converged (no changes needed).", i)
                return last_good

            updated = _apply_updates(last_good, critique)
            if len(updated) > MAX_CRITERIA:
                logger.warning(
                    "Critic iter %d proposed %d criteria; capping at MAX_CRITERIA=%d.",
                    i, len(updated), MAX_CRITERIA,
                )
                updated = updated[:MAX_CRITERIA]
            last_good = updated
        except Exception as e:  # noqa: BLE001 — broad on purpose; loop must never poison the result
            logger.warning("Critic iter %d failed: %s. Returning last-good criteria.", i, e)
            return last_good

    return last_good


async def handle_generate_judge(
    bedrock: BedrockClient, args: Dict[str, Any]
) -> List[TextContent]:
    """Handle generate_judge tool call.

    Generates custom evaluation criteria based on QA pairs for Jury multi-judge.

    Args:
        bedrock: Bedrock client instance
        args: Tool arguments containing dataset (name), domain, and user_id

    Returns:
        MCP TextContent response with judge_id and name
    """
    dataset_name = args.get("dataset")
    domain = args.get("domain", "general")
    user_id = args.get("user_id")

    # Validate required args
    if not user_id:
        return [
            TextContent(
                type="text",
                text=json.dumps({"success": False, "error": "user_id is required"}),
            )
        ]
    if not dataset_name:
        return [
            TextContent(
                type="text",
                text=json.dumps({
                    "success": False,
                    "error": "dataset is required - use list_datasets to see available datasets",
                }),
            )
        ]

    try:
        # Load dataset from database
        dataset_data = get_dataset_by_name(user_id, dataset_name)
        if not dataset_data:
            return [
                TextContent(
                    type="text",
                    text=json.dumps({
                        "success": False,
                        "error": f"Dataset '{dataset_name}' not found. Use list_datasets to see available datasets.",
                    }),
                )
            ]

        tests = dataset_data.get("tests", [])

        # Convert test cases to qa_pairs format
        qa_pairs = [
            {"question": tc["vars"]["question"], "golden_answer": tc["vars"]["golden_answer"]}
            for tc in tests
        ]

        if not qa_pairs:
            return [
                TextContent(
                    type="text",
                    text=json.dumps({
                        "success": False,
                        "error": "No QA pairs provided",
                    }),
                )
            ]

        # Generate criteria (analyzes up to 10 QA pairs)
        criteria_result = await generate_judge(bedrock, qa_pairs, domain)
        criteria = criteria_result.get("criteria", [])

        if not criteria:
            return [
                TextContent(
                    type="text",
                    text=json.dumps({
                        "success": False,
                        "error": "Failed to generate criteria",
                    }),
                )
            ]

        # Refine via critic-led loop: scores a sample, prunes redundancy,
        # adds missing failure modes. Crash-safe — any iteration error
        # returns the last-good set, so the user never gets fewer criteria
        # than the initial generation produced.
        criteria = await refine_criteria_loop(bedrock, criteria, qa_pairs, domain)

        # Save to user's database
        safe_name = "".join(c if c.isalnum() or c in (' ', '-', '_') else '_' for c in domain[:50])
        safe_name = safe_name.strip().replace(' ', '_').lower()
        judge_name = f"{safe_name}_criteria"

        judge_config = {
            "domain": domain,
            "criteria": criteria,
            "samples_analyzed": min(len(qa_pairs), 10),
        }
        judge_id = save_judge_to_db(user_id, judge_name, judge_config)

        # Format criteria for display
        criteria_preview = "\n".join([
            f"  - {c['name']}: {c['description']}"
            for c in criteria
        ])

        result = {
            "success": True,
            "judge_id": judge_id,
            "name": judge_name,
            "domain": domain,
            "criteria_count": len(criteria),
            "criteria": criteria_preview,
            "samples_analyzed": min(len(qa_pairs), 10),
        }

        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    except Exception as e:
        return [
            TextContent(
                type="text",
                text=json.dumps({
                    "success": False,
                    "error": f"Failed to generate criteria: {str(e)}",
                }),
            )
        ]
