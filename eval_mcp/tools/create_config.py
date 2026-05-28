"""Create Inspect AI evaluation task files with multi-judge jury scoring.

Generates:
- A Python task file that uses Inspect AI's eval framework
- A JSON config file with rubric, criteria, judge models, and dataset path

The default scorer is a multi-judge jury (binary per criterion, majority
vote). Customers can also opt into Inspect AI's deterministic built-in
scorers (``f1``, ``exact``, ``includes``, ``match``) via the ``scorers``
argument — alone or composed with the jury.
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp.types import TextContent

from eval_mcp.core.judge_config import JudgeConfig
from eval_mcp.core.user_storage import (
    get_judge_by_name,
    get_dataset_by_name,
    get_user_dir,
)


# ---------------------------------------------------------------------------
# Scorer registry — names accepted on the ``scorers`` parameter
# ---------------------------------------------------------------------------
#
# "jury" is implemented inline in the generated task file (see
# JURY_SCORER_BLOCK below). All other entries map to Inspect AI's built-in
# scorers, which work directly against the ``(question, golden_answer,
# completion)`` dataset shape this MCP already produces.

# Each entry maps a public scorer name (the value users pass on the
# ``scorers`` parameter) to:
#   - expr:   the Python expression to emit into Task(scorer=[...])
#   - import: the symbol to import (None when the scorer is defined inline
#             in the generated task file, like jury)
#   - module: where to import the symbol from. ``inspect_ai.scorer`` for
#             the built-ins, ``eval_mcp.scorers.rag`` for the RAG suite.
SCORER_REGISTRY: Dict[str, Dict[str, Any]] = {
    "jury": {"expr": "jury_scorer()", "import": None, "module": None},
    "f1": {"expr": "f1()", "import": "f1", "module": "inspect_ai.scorer"},
    "exact": {"expr": "exact()", "import": "exact", "module": "inspect_ai.scorer"},
    "includes": {"expr": "includes()", "import": "includes", "module": "inspect_ai.scorer"},
    "match": {"expr": "match()", "import": "match", "module": "inspect_ai.scorer"},
    "faithfulness": {
        "expr": "faithfulness()",
        "import": "faithfulness",
        "module": "eval_mcp.scorers.rag",
    },
    "answer_relevancy": {
        "expr": "answer_relevancy()",
        "import": "answer_relevancy",
        "module": "eval_mcp.scorers.rag",
    },
    "contextual_precision": {
        "expr": "contextual_precision()",
        "import": "contextual_precision",
        "module": "eval_mcp.scorers.rag",
    },
    "contextual_recall": {
        "expr": "contextual_recall()",
        "import": "contextual_recall",
        "module": "eval_mcp.scorers.rag",
    },
    "contextual_relevancy": {
        "expr": "contextual_relevancy()",
        "import": "contextual_relevancy",
        "module": "eval_mcp.scorers.rag",
    },
    "groundedness": {
        "expr": "groundedness()",
        "import": "groundedness",
        "module": "eval_mcp.scorers.rag",
    },
}

# Names that require ``retrieval_context`` on every sample. Used to
# (a) opt into the RAG-aware solver and (b) fail-fast in the dataset
# step if the column isn't present.
RAG_SCORERS = frozenset({
    "faithfulness",
    "answer_relevancy",
    "contextual_precision",
    "contextual_recall",
    "contextual_relevancy",
    "groundedness",
})

DEFAULT_SCORERS: List[str] = ["jury"]


def has_rag_scorer(scorers: List[str]) -> bool:
    return any(s in RAG_SCORERS for s in scorers)


def _validate_scorers(scorers: Optional[List[str]]) -> List[str]:
    """Normalize/validate the ``scorers`` argument.

    Empty/None falls back to the default (jury). Unknown names raise
    ``ValueError`` so the caller can surface a clean error to the user.
    Order is preserved; duplicates are dropped.
    """
    if not scorers:
        return list(DEFAULT_SCORERS)
    unknown = [s for s in scorers if s not in SCORER_REGISTRY]
    if unknown:
        valid = ", ".join(sorted(SCORER_REGISTRY.keys()))
        raise ValueError(
            f"Unknown scorer(s): {unknown}. Choose from: {valid}"
        )
    seen: set = set()
    ordered: List[str] = []
    for s in scorers:
        if s not in seen:
            seen.add(s)
            ordered.append(s)
    return ordered


def _render_scorer_expression(scorers: List[str]) -> str:
    """Render the value passed to ``Task(scorer=...)``."""
    exprs = [SCORER_REGISTRY[s]["expr"] for s in scorers]
    if len(exprs) == 1:
        return exprs[0]
    return "[" + ", ".join(exprs) + "]"


def _render_builtin_scorer_imports(scorers: List[str]) -> str:
    """Render extra ``from <module> import …`` lines for built-in and
    library scorers, one line per module. The jury defines its own
    scorer inline so it doesn't contribute to these imports.

    The render is stable per module — sorted names within a module,
    modules sorted alphabetically — so diffs of the generated task file
    stay legible across runs.
    """
    by_module: Dict[str, List[str]] = {}
    for s in scorers:
        entry = SCORER_REGISTRY[s]
        name = entry["import"]
        module = entry.get("module")
        if not name or not module:
            continue
        by_module.setdefault(module, []).append(name)
    if not by_module:
        return ""
    lines: List[str] = []
    for module in sorted(by_module):
        unique = sorted(set(by_module[module]))
        lines.append(f"from {module} import {', '.join(unique)}")
    # Inject the RAG judge-model wiring AND the solver import once when
    # any RAG scorer is selected. Picks the first model from
    # CONFIG["judge_models"] — mirrors what jury_scorer iterates over,
    # so users see the SAME judge labels in cost reports regardless of
    # which scorer ran.
    if any(s in RAG_SCORERS for s in scorers):
        lines.append(
            "from eval_mcp.scorers.rag import configure_judge as _rag_configure_judge, rag_prompt_solver"
        )
    return "\n".join(lines)


def build_judge_system_prompt(criteria: List[Dict[str, str]]) -> str:
    """Build the judge system prompt from criteria.

    Includes the per-criterion improvement-note protocol used by the
    prompt optimizer: when a criterion scores 0, the judge fills in a
    sibling ``<criterion>_improvement`` field with a one-sentence hint
    about what the answer should change. The optimizer reads these
    notes when proposing a better prompt.
    """
    criteria_lines = "\n".join([
        f"- {c['name']}: {c['description']}"
        for c in criteria
    ])

    return (
        "You are a judge evaluating an AI answer against a reference answer.\n"
        "Score each criterion as 1 (pass) or 0 (fail), "
        "then call the submit_scores tool with your scores.\n\n"
        "Whenever you score a criterion 0, also fill in its sibling "
        "<criterion>_improvement field with ONE short sentence describing "
        "what the answer should change to score 1. Leave it empty when "
        "you score 1.\n\n"
        f"Criteria:\n{criteria_lines}"
    )


def build_config_json(
    dataset_path: str,
    providers: List[str],
    judge_config: JudgeConfig,
    description: Optional[str] = None,
    prompts: Optional[List[str]] = None,
    scorers: Optional[List[str]] = None,
    score_only: bool = False,
) -> dict:
    """Build the JSON config that the task file will load.

    ``score_only`` flips the config into "score pre-generated outputs"
    mode: ``providers`` is allowed to be empty (no candidate model is
    invoked), and ``run_eval.py`` skips the ``--model`` subprocess flag.
    """
    config = {
        "dataset_path": dataset_path,
        "providers": providers,
        "judge_models": dict(judge_config.judges),
        "criteria": judge_config.criteria,
        "system_prompt": build_judge_system_prompt(judge_config.criteria),
        "description": description or "",
        "scorers": scorers or list(DEFAULT_SCORERS),
    }
    if prompts and len(prompts) > 1:
        config["prompts"] = prompts
    if score_only:
        config["score_only"] = True
    return config


# ---------------------------------------------------------------------------
# Task-file template parts
# ---------------------------------------------------------------------------
#
# ``TASK_FILE_BASE`` is emitted for every config. ``JURY_SCORER_BLOCK`` is
# appended only when ``"jury"`` is in the scorers list. The task definition
# template (SINGLE / PROMPT) is parameterised by ``{scorer_expr}``.

TASK_FILE_BASE = '''"""Inspect AI evaluation task: {config_name}

Auto-generated. Scorers: {scorers_doc}{mode_doc}
"""

import json
from pathlib import Path

from inspect_ai import Task, task
from inspect_ai.dataset import json_dataset, FieldSpec
from inspect_ai.solver import generate, prompt_template
{extra_imports}
_config_path = Path(__file__).with_suffix(".json")
CONFIG = json.loads(_config_path.read_text())

DATASET_PATH = CONFIG["dataset_path"]
PROVIDERS = CONFIG.get("providers", [])
{rag_init}'''


JURY_SCORER_BLOCK = '''
from inspect_ai.model import ChatMessageUser, ChatMessageSystem, get_model
from inspect_ai.scorer import Score, mean, scorer, stderr
from inspect_ai.tool._tool_info import ToolInfo
from inspect_ai.tool._tool_params import ToolParams

JUDGE_MODELS = CONFIG["judge_models"]
CRITERIA = CONFIG["criteria"]
SYSTEM_PROMPT = CONFIG["system_prompt"]


def _build_scoring_tool():
    # Schema is intentionally flat: each criterion gets a sibling
    # `<name>_improvement` string slot. Nested objects-per-criterion would
    # be cleaner but Inspect's tool-forced output handles flat int/string
    # fields most reliably across models. Improvement slots are optional —
    # old judge runs without them still parse fine.
    properties = {}
    required = []
    for c in CRITERIA:
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


def _extract_scores(output, criteria_names):
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
        return None, None, None, f"No submit_scores tool call found"

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
def jury_scorer():
    async def score(state, target):
        output = state.output.completion if state.output else ""
        if not output:
            return Score(value=0.0, answer="", explanation="No output generated")

        question = str(state.input)
        golden = target.text if target else ""
        criteria_names = [c["name"] for c in CRITERIA]
        tool = _build_scoring_tool()

        votes = {n: [] for n in criteria_names}
        # Per-criterion improvement hints collected from judges that
        # scored 0. List of {judge, note} pairs so downstream
        # consumers (optimizer, report) can attribute hints to judges
        # and de-dupe across them.
        improvements_per_criterion = {n: [] for n in criteria_names}
        details = []
        errors = []

        for label, model_id in JUDGE_MODELS.items():
            try:
                judge = get_model(model_id)
                result = await judge.generate(
                    [
                        ChatMessageSystem(content=SYSTEM_PROMPT),
                        ChatMessageUser(
                            content=f"Question:\\n{question}\\n\\nAI Answer:\\n{output}\\n\\nReference Answer:\\n{golden}"
                        ),
                    ],
                    tools=[tool],
                    tool_choice="any",
                )

                scores, reason, improvements, err = _extract_scores(result, criteria_names)
                if scores is not None:
                    for n in criteria_names:
                        votes[n].append(scores[n])
                        if scores[n] == 0 and improvements and improvements.get(n):
                            improvements_per_criterion[n].append(
                                {"judge": label, "note": improvements[n]}
                            )
                    details.append(f"  {label}: {scores} - {reason}")
                else:
                    errors.append(f"  {label}: {err}")
                    details.append(f"  {label}: EXCLUDED ({err[:80]})")
            except Exception as e:
                errors.append(f"  {label}: {str(e)[:200]}")
                details.append(f"  {label}: ERROR ({str(e)[:80]})")

        results = []
        for n in criteria_names:
            v = votes[n]
            if not v:
                results.append({"name": n, "votes_for": 0, "total": 0, "score": 0.0, "note": "no valid responses"})
            else:
                vf = sum(v)
                entry = {"name": n, "votes_for": vf, "total": len(v), "score": vf / len(v)}
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

        return Score(
            value=jury_score,
            answer=output[:200],
            explanation="\\n".join(lines),
            metadata={"jury_score": jury_score, "criteria_results": results},
        )

    return score
'''


SINGLE_TASK_TEMPLATE = '''
@task
def eval_task():
    return Task(
        dataset=json_dataset(DATASET_PATH, FieldSpec(input="question", target="golden_answer"{field_spec_metadata})),
        solver=[{solver_chain}],
        scorer={scorer_expr},
    )
'''

PROMPT_TASK_TEMPLATE = '''
@task
def eval_{index}():
    return Task(
        dataset=json_dataset(DATASET_PATH, FieldSpec(input="question", target="golden_answer"{field_spec_metadata})),
        solver=[prompt_template({prompt_repr}), {solver_chain}],
        scorer={scorer_expr},
    )
'''


def create_inspect_task_file(
    dataset_path: str,
    providers: List[str],
    config_name: str,
    config_dir: str,
    judge_config: JudgeConfig,
    description: Optional[str] = None,
    prompts: Optional[List[str]] = None,
    scorers: Optional[List[str]] = None,
    score_only: bool = False,
) -> tuple[str, dict]:
    """Create task file code and config JSON.

    When ``score_only`` is True the task file imports
    ``static_output_solver`` and runs it instead of ``generate()``;
    each sample's ``actual_output`` metadata field is written directly
    into ``state.output`` so scorers run against the pre-generated answer.

    Returns:
        (task_code, config_dict) — caller writes both to disk.
    """
    scorers = _validate_scorers(scorers)
    config_data = build_config_json(
        dataset_path, providers, judge_config, description, prompts, scorers,
        score_only=score_only,
    )

    extra_imports_parts: List[str] = []
    builtin = _render_builtin_scorer_imports(scorers)
    if builtin:
        extra_imports_parts.append(builtin)
    if score_only:
        extra_imports_parts.append(
            "from eval_mcp.solvers.static_output import static_output_solver"
        )
    extra_imports = "\n".join(extra_imports_parts)
    scorer_expr = _render_scorer_expression(scorers)

    rag_enabled = has_rag_scorer(scorers)
    rag_init = ""
    if rag_enabled:
        # Picks the first judge model so RAG scorers share whatever the
        # user already configured for the jury. ``next(iter(...))`` keeps
        # the order stable (Python dict insertion order).
        rag_init = (
            "\n# Wire RAG scorers up to the same judge model the jury uses.\n"
            "if CONFIG.get(\"judge_models\"):\n"
            "    _rag_configure_judge(next(iter(CONFIG[\"judge_models\"].values())))\n"
        )

    # FieldSpec metadata combines both modes. Order is stable for diff
    # legibility: score-only first, RAG second.
    metadata_keys: List[str] = []
    if score_only:
        metadata_keys.append("actual_output")
    if rag_enabled:
        metadata_keys.append("retrieval_context")
    field_spec_metadata = (
        ", metadata=[" + ", ".join(f'"{k}"' for k in metadata_keys) + "]"
        if metadata_keys
        else ""
    )

    # Solver chain:
    # - score-only: static_output_solver alone — no model call, so
    #   rag_prompt_solver (which rewrites the model's prompt) is moot
    #   and skipped even when RAG scorers are selected.
    # - RAG (live model): inject chunks via rag_prompt_solver, then generate.
    # - default: plain generate.
    if score_only:
        solver_chain = "static_output_solver()"
    elif rag_enabled:
        solver_chain = "rag_prompt_solver(), generate()"
    else:
        solver_chain = "generate()"

    mode_doc = ""
    if score_only:
        mode_doc = "\nMode: score-only (no candidate model invoked)."
    elif rag_enabled:
        mode_doc = "\nMode: RAG (retrieval_context injected into prompt)."

    parts: List[str] = []
    parts.append(TASK_FILE_BASE.format(
        config_name=config_name,
        scorers_doc=", ".join(scorers),
        extra_imports=(extra_imports + "\n") if extra_imports else "",
        mode_doc=mode_doc,
        rag_init=rag_init,
    ))
    if "jury" in scorers:
        parts.append(JURY_SCORER_BLOCK)

    # Apply prompt_template whenever a non-default prompt is given, even
    # for a single prompt. The old guard `len > 1` silently dropped
    # single custom templates — caller passes a wrapper, Inspect runs
    # without it. The optimizer triggered this (it always evaluates one
    # candidate prompt at a time); single-prompt evals created from the
    # chat agent path hit the same latent bug.
    has_custom_prompt = bool(prompts) and any(p and p != "{question}" for p in prompts)
    if has_custom_prompt:
        for i, prompt in enumerate(prompts):
            # prompt_template() uses {prompt} as the placeholder for input text
            normalized = prompt.replace("{question}", "{prompt}")
            parts.append(PROMPT_TASK_TEMPLATE.format(
                index=i + 1,
                prompt_repr=repr(normalized),
                scorer_expr=scorer_expr,
                field_spec_metadata=field_spec_metadata,
                solver_chain=solver_chain,
            ))
    else:
        parts.append(SINGLE_TASK_TEMPLATE.format(
            scorer_expr=scorer_expr,
            field_spec_metadata=field_spec_metadata,
            solver_chain=solver_chain,
        ))

    return "".join(parts), config_data


async def handle_create_eval_config(args: Dict[str, Any]) -> List[TextContent]:
    """Handle create_eval_config tool call."""
    try:
        import time
        dataset_name = args.get("dataset")
        judge_name = args.get("judge")
        providers = args.get("providers")
        # Auto-generated timestamp-based name. No agent-chosen names — that's
        # how stale configs get reused by accident.
        config_name = f"eval_{int(time.time() * 1000)}"
        description = args.get("description")
        user_id = args.get("user_id")
        scorers_arg = args.get("scorers")

        if not user_id:
            return [TextContent(type="text", text=json.dumps({"success": False, "error": "user_id is required"}))]
        if not dataset_name:
            return [TextContent(type="text", text=json.dumps({"success": False, "error": "dataset is required"}))]
        if not judge_name:
            return [TextContent(type="text", text=json.dumps({"success": False, "error": "judge is required"}))]
        # ``providers`` validity is checked AFTER we know whether this is a
        # score-only dataset — in score-only mode no candidate model is
        # invoked and providers can be empty.

        try:
            scorers = _validate_scorers(scorers_arg)
        except ValueError as e:
            return [TextContent(type="text", text=json.dumps({"success": False, "error": str(e)}))]

        judge_data = get_judge_by_name(user_id, judge_name)
        if not judge_data:
            return [TextContent(type="text", text=json.dumps({"success": False, "error": f"Judge '{judge_name}' not found"}))]

        criteria = judge_data["config"].get("criteria")
        if not criteria:
            return [TextContent(type="text", text=json.dumps({"success": False, "error": f"Judge '{judge_name}' has no criteria"}))]

        judge_models_arg = args.get("judge_models")
        custom_judges = {m: m for m in judge_models_arg} if judge_models_arg else None
        judge_config = JudgeConfig(criteria=criteria, judges=custom_judges)

        dataset_data = get_dataset_by_name(user_id, dataset_name)
        if not dataset_data:
            return [TextContent(type="text", text=json.dumps({"success": False, "error": f"Dataset '{dataset_name}' not found"}))]

        tests = dataset_data.get("tests", [])
        if not tests:
            return [TextContent(type="text", text=json.dumps({"success": False, "error": f"Dataset '{dataset_name}' is empty"}))]

        # Write dataset JSON
        user_dir = get_user_dir(user_id)
        temp_dir = user_dir / "temp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        dataset_file = temp_dir / f"{dataset_name}.json"

        rag_enabled = has_rag_scorer(scorers)

        # Detect score-only mode from the dataset itself: if EVERY sample
        # carries an actual_output, we score the pre-generated answers
        # without invoking a candidate model. Mixed datasets (some with,
        # some without) are refused — the contract is all-or-none so the
        # subprocess invocation can deterministically skip --model.
        rows_with_ao = 0
        rows_without_ao_indices: List[int] = []
        for i, test in enumerate(tests):
            v = test.get("vars", test)
            ao = v.get("actual_output")
            if isinstance(ao, str) and ao.strip():
                rows_with_ao += 1
            else:
                rows_without_ao_indices.append(i)

        score_only = rows_with_ao > 0 and not rows_without_ao_indices
        if rows_with_ao > 0 and rows_without_ao_indices:
            return [TextContent(type="text", text=json.dumps({
                "success": False,
                "error": (
                    f"Dataset '{dataset_name}' has {rows_with_ao}/{len(tests)} samples "
                    f"with actual_output. Score-only mode requires every sample to have "
                    f"actual_output (or none — in which case the candidate model is "
                    f"invoked). Mixed datasets are not supported. Missing rows: "
                    f"{rows_without_ao_indices[:5]}"
                    + (" ..." if len(rows_without_ao_indices) > 5 else "")
                ),
            }))]

        if not score_only and not providers:
            return [TextContent(type="text", text=json.dumps({
                "success": False,
                "error": (
                    "At least one provider is required when the dataset has no "
                    "actual_output column. To score pre-generated answers without "
                    "calling a candidate model, re-upload the dataset via "
                    "save_dataset with an actual_output column mapping."
                ),
            }))]

        inspect_samples = []
        missing_rc_indices: List[int] = []
        for i, test in enumerate(tests):
            v = test.get("vars", test)
            sample: Dict[str, Any] = {
                "question": v.get("question", ""),
                "golden_answer": v.get("golden_answer", ""),
            }
            if score_only:
                sample["actual_output"] = v.get("actual_output", "")
            rc = v.get("retrieval_context")
            if rc:
                sample["retrieval_context"] = rc
            elif rag_enabled:
                missing_rc_indices.append(i)
            inspect_samples.append(sample)

        if rag_enabled and missing_rc_indices:
            return [TextContent(type="text", text=json.dumps({
                "success": False,
                "error": (
                    f"Dataset '{dataset_name}' has {len(missing_rc_indices)} sample(s) "
                    f"without retrieval_context (first missing index: {missing_rc_indices[0]}). "
                    f"RAG scorers {sorted(set(scorers) & RAG_SCORERS)} require a "
                    f"retrieval_context column (list[str]) on every sample. Re-upload "
                    f"the dataset via save_dataset with a retrieval_context column."
                ),
            }))]

        with open(dataset_file, "w") as f:
            json.dump(inspect_samples, f, indent=2)

        # Normalize prompts
        prompts_arg = args.get("prompts")
        prompts: Optional[List[str]] = None
        if isinstance(prompts_arg, list) and len(prompts_arg) > 1:
            prompts = prompts_arg
        elif isinstance(prompts_arg, str) and prompts_arg != "{question}":
            prompts = [prompts_arg]

        # Generate task file + config JSON
        config_dir = user_dir / "configs"
        config_dir.mkdir(parents=True, exist_ok=True)

        task_code, config_data = create_inspect_task_file(
            dataset_path=str(dataset_file),
            providers=providers or [],
            config_name=config_name,
            config_dir=str(config_dir),
            description=description,
            judge_config=judge_config,
            prompts=prompts,
            scorers=scorers,
            score_only=score_only,
        )

        # Write both files
        (config_dir / f"{config_name}.py").write_text(task_code)
        (config_dir / f"{config_name}.json").write_text(json.dumps(config_data, indent=2))

        result = {
            "success": True,
            "configName": config_name,
            "summary": {
                "dataset": dataset_name,
                "judge": judge_name,
                "providers": len(providers or []),
                "testCases": len(tests),
                "judges": list(judge_config.judges.keys()),
                "criteria": [c["name"] for c in criteria],
                "scorers": scorers,
                "prompts": len(prompts) if prompts else 1,
                "description": description or f"Evaluation: {config_name}",
            },
            "nextStep": f"Run evaluation: run_evaluation(configName='{config_name}')",
        }
        if score_only:
            result["summary"]["mode"] = "score-only"

        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    except Exception as e:
        return [TextContent(type="text", text=json.dumps({"success": False, "error": f"Failed to create config: {str(e)}"}))]
