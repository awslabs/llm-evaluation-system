"""Create Inspect AI evaluation task files with multi-judge jury scoring.

Generates:
- A Python task file that uses Inspect AI's eval framework
- A JSON config file with rubric, criteria, judge models, and dataset path

Each judge is forced to call a scoring tool with per-criterion binary scores.
Results aggregated via hierarchical majority voting.
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp.types import TextContent

from backend.core.judge_config import JudgeConfig
from backend.core.user_storage import (
    get_judge_by_name,
    get_dataset_by_name,
    get_user_dir,
)


def build_judge_system_prompt(criteria: List[Dict[str, str]]) -> str:
    """Build the judge system prompt from criteria."""
    criteria_lines = "\n".join([
        f"- {c['name']}: {c['description']}"
        for c in criteria
    ])

    return (
        "You are a judge evaluating an AI answer against a reference answer.\n"
        "Score each criterion as 1 (pass) or 0 (fail), "
        "then call the submit_scores tool with your scores.\n\n"
        f"Criteria:\n{criteria_lines}"
    )


def build_config_json(
    dataset_path: str,
    providers: List[str],
    judge_config: JudgeConfig,
    description: Optional[str] = None,
) -> dict:
    """Build the JSON config that the task file will load."""
    return {
        "dataset_path": dataset_path,
        "providers": providers,
        "judge_models": dict(judge_config.judges),
        "criteria": judge_config.criteria,
        "system_prompt": build_judge_system_prompt(judge_config.criteria),
        "description": description or "",
    }


TASK_FILE_TEMPLATE = '''"""Inspect AI evaluation task: {config_name}

Auto-generated. Uses multi-judge jury scoring with tool-forced structured output.
"""

import json
from pathlib import Path

from inspect_ai import Task, task
from inspect_ai.dataset import json_dataset, FieldSpec
from inspect_ai.model import ChatMessageUser, ChatMessageSystem, get_model
from inspect_ai.scorer import Score, accuracy, scorer, stderr
from inspect_ai.solver import generate

from inspect_ai.tool._tool_info import ToolInfo
from inspect_ai.tool._tool_params import ToolParams

_config_path = Path(__file__).with_suffix(".json")
CONFIG = json.loads(_config_path.read_text())

DATASET_PATH = CONFIG["dataset_path"]
PROVIDERS = CONFIG["providers"]
JUDGE_MODELS = CONFIG["judge_models"]
CRITERIA = CONFIG["criteria"]
SYSTEM_PROMPT = CONFIG["system_prompt"]


def _build_scoring_tool():
    properties = {{}}
    required = []
    for c in CRITERIA:
        properties[c["name"]] = {{
            "type": "integer",
            "description": f"Score for {{c['name']}}: 1 if pass, 0 if fail",
            "enum": [0, 1],
        }}
        required.append(c["name"])
    properties["reason"] = {{
        "type": "string",
        "description": "Brief explanation of the scoring decision",
    }}
    required.append("reason")

    return ToolInfo(
        name="submit_scores",
        description="Submit binary scores for each evaluation criterion",
        parameters=ToolParams(type="object", properties=properties, required=required),
    )


def _extract_scores(output, criteria_names):
    if not output or not output.message or not output.message.tool_calls:
        text = output.completion[:200] if output and output.completion else "(empty)"
        return None, None, f"No tool call. Response: {{text}}"

    # Merge all submit_scores tool calls (some models split across multiple calls)
    args = {{}}
    for tc in output.message.tool_calls:
        if tc.function == "submit_scores":
            args.update(tc.arguments)

    if not args:
        return None, None, f"No submit_scores tool call found"

    missing = [n for n in criteria_names if n not in args]
    if missing:
        return None, None, f"Missing criteria: {{missing}}. Got: {{list(args.keys())}}"

    scores = {{n: int(bool(args[n])) for n in criteria_names}}
    return scores, args.get("reason", ""), None


@scorer(metrics=[accuracy(), stderr()])
def jury_scorer():
    async def score(state, target):
        output = state.output.completion if state.output else ""
        if not output:
            return Score(value="I", answer="", explanation="No output generated")

        question = str(state.input)
        golden = target.text if target else ""
        criteria_names = [c["name"] for c in CRITERIA]
        tool = _build_scoring_tool()

        votes = {{n: [] for n in criteria_names}}
        details = []
        errors = []

        for label, model_id in JUDGE_MODELS.items():
            try:
                judge = get_model(model_id)
                result = await judge.generate(
                    [
                        ChatMessageSystem(content=SYSTEM_PROMPT),
                        ChatMessageUser(
                            content=f"Question:\\n{{question}}\\n\\nAI Answer:\\n{{output}}\\n\\nReference Answer:\\n{{golden}}"
                        ),
                    ],
                    tools=[tool],
                    tool_choice="auto",
                )

                scores, reason, err = _extract_scores(result, criteria_names)
                if scores is not None:
                    for n in criteria_names:
                        votes[n].append(scores[n])
                    details.append(f"  {{label}}: {{scores}} - {{reason}}")
                else:
                    errors.append(f"  {{label}}: {{err}}")
                    details.append(f"  {{label}}: EXCLUDED ({{err[:80]}})")
            except Exception as e:
                errors.append(f"  {{label}}: {{str(e)[:200]}}")
                details.append(f"  {{label}}: ERROR ({{str(e)[:80]}})")

        # Per-criterion majority vote
        results = []
        for n in criteria_names:
            v = votes[n]
            if not v:
                results.append({{"name": n, "votes_for": 0, "total": 0, "passed": False, "note": "no valid responses"}})
            else:
                vf = sum(v)
                results.append({{"name": n, "votes_for": vf, "total": len(v), "passed": vf > len(v) / 2}})

        # Overall pass if >50% criteria pass
        n_passed = sum(1 for r in results if r["passed"])
        n_total = len(criteria_names)
        jury_score = n_passed / max(n_total, 1)
        passed = jury_score > 0.5

        lines = [f"Jury: {{'PASS' if passed else 'FAIL'}} ({{n_passed}}/{{n_total}} criteria)", ""]
        for r in results:
            s = "PASS" if r["passed"] else "FAIL"
            extra = f" - {{r['note']}}" if "note" in r else ""
            lines.append(f"  {{r['name']}}: {{s}} ({{r['votes_for']}}/{{r['total']}}){{extra}}")
        lines += ["", "Judges:"] + details
        if errors:
            lines += ["", "Errors:"] + errors

        return Score(
            value="C" if passed else "I",
            answer=output[:200],
            explanation="\\n".join(lines),
            metadata={{"jury_score": jury_score, "criteria_passed": n_passed, "criteria_total": n_total, "criteria_results": results}},
        )

    return score


@task
def eval_task():
    return Task(
        dataset=json_dataset(DATASET_PATH, FieldSpec(input="question", target="golden_answer")),
        solver=[generate()],
        scorer=jury_scorer(),
    )
'''


def create_inspect_task_file(
    dataset_path: str,
    providers: List[str],
    config_name: str,
    config_dir: str,
    judge_config: JudgeConfig,
    description: Optional[str] = None,
) -> tuple[str, dict]:
    """Create task file code and config JSON.

    Returns:
        (task_code, config_dict) — caller writes both to disk.
    """
    config_data = build_config_json(dataset_path, providers, judge_config, description)
    task_code = TASK_FILE_TEMPLATE.format(config_name=config_name)
    return task_code, config_data


async def handle_create_eval_config(args: Dict[str, Any]) -> List[TextContent]:
    """Handle create_eval_config tool call."""
    try:
        dataset_name = args.get("dataset")
        judge_name = args.get("judge")
        providers = args.get("providers")
        config_name = args.get("configName", "evaluation")
        description = args.get("description")
        user_id = args.get("user_id")

        if not user_id:
            return [TextContent(type="text", text=json.dumps({"success": False, "error": "user_id is required"}))]
        if not dataset_name:
            return [TextContent(type="text", text=json.dumps({"success": False, "error": "dataset is required"}))]
        if not judge_name:
            return [TextContent(type="text", text=json.dumps({"success": False, "error": "judge is required"}))]
        if not providers:
            return [TextContent(type="text", text=json.dumps({"success": False, "error": "At least one provider is required"}))]

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

        inspect_samples = []
        for test in tests:
            v = test.get("vars", test)
            inspect_samples.append({
                "question": v.get("question", ""),
                "golden_answer": v.get("golden_answer", ""),
            })

        with open(dataset_file, "w") as f:
            json.dump(inspect_samples, f, indent=2)

        # Generate task file + config JSON
        config_dir = user_dir / "configs"
        config_dir.mkdir(parents=True, exist_ok=True)

        task_code, config_data = create_inspect_task_file(
            dataset_path=str(dataset_file),
            providers=providers,
            config_name=config_name,
            config_dir=str(config_dir),
            description=description,
            judge_config=judge_config,
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
                "providers": len(providers),
                "testCases": len(tests),
                "judges": list(judge_config.judges.keys()),
                "criteria": [c["name"] for c in criteria],
                "description": description or f"Evaluation: {config_name}",
            },
            "nextStep": f"Run evaluation: run_evaluation(configName='{config_name}')",
        }

        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    except Exception as e:
        return [TextContent(type="text", text=json.dumps({"success": False, "error": f"Failed to create config: {str(e)}"}))]
