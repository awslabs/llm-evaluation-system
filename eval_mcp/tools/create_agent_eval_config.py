"""Create Inspect AI agent evaluation task files.

Generates task files that use sandbox_agent_bridge() to evaluate
arbitrary agent containers. The user provides a container image URI
and an entrypoint command. Inspect handles all LLM interception,
tool call capture, and scoring.
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
from eval_mcp.tools.create_config import (
    _render_builtin_scorer_imports,
    _render_scorer_expression,
    _validate_scorers,
)


AGENT_TASK_BASE = '''"""Inspect AI agent evaluation task: {config_name}

Auto-generated. Evaluates an agent running in a container via sandbox_agent_bridge().
All LLM calls made by the agent are intercepted and recorded.

Scorers: {scorers_doc}
"""

import json
from pathlib import Path

from inspect_ai import Task, task
from inspect_ai.agent import Agent, AgentState, agent, sandbox_agent_bridge
from inspect_ai.dataset import json_dataset, FieldSpec
from inspect_ai.model import ChatMessageUser
from inspect_ai.util import sandbox
{extra_imports}
_config_path = Path(__file__).with_suffix(".json")
CONFIG = json.loads(_config_path.read_text())

DATASET_PATH = CONFIG["dataset_path"]
AGENT_CMD = CONFIG["agent_cmd"]


@agent
def agent_solver() -> Agent:
    """Run the user's agent in a container with LLM interception."""

    async def execute(state: AgentState) -> AgentState:
        async with sandbox_agent_bridge(state, model="inspect") as bridge:
            prompt = ""
            for msg in reversed(state.messages):
                if isinstance(msg, ChatMessageUser):
                    prompt = msg.text
                    break

            result = await sandbox().exec(
                cmd=AGENT_CMD + [prompt],
                env={{
                    "OPENAI_BASE_URL": f"http://localhost:{{bridge.port}}/v1",
                    "OPENAI_API_KEY": "not-needed",
                    "ANTHROPIC_BASE_URL": f"http://localhost:{{bridge.port}}",
                    "ANTHROPIC_API_KEY": "not-needed",
                }},
                timeout=300,
            )

            if not result.success:
                raise RuntimeError(
                    f"Agent failed (exit {{result.returncode}}):\\n{{result.stderr[:1000]}}"
                )

        return bridge.state

    return execute
'''


AGENT_JURY_BLOCK = '''
from inspect_ai.model import ChatMessageSystem, get_model
from inspect_ai.scorer import Score, mean, scorer, stderr
from inspect_ai.tool._tool_info import ToolInfo
from inspect_ai.tool._tool_params import ToolParams

JUDGE_MODELS = CONFIG["judge_models"]
CRITERIA = CONFIG["criteria"]
SYSTEM_PROMPT = CONFIG["system_prompt"]


def _build_scoring_tool():
    properties = {}
    required = []
    for c in CRITERIA:
        properties[c["name"]] = {
            "type": "integer",
            "description": f"Score for {c['name']}: 1 if pass, 0 if fail",
            "enum": [0, 1],
        }
        required.append(c["name"])
    properties["reason"] = {
        "type": "string",
        "description": "Brief explanation of the scoring decision",
    }
    required.append("reason")

    return ToolInfo(
        name="submit_scores",
        description="Submit binary scores for each evaluation criterion",
        parameters=ToolParams(type="object", properties=properties, required=required),
    )


def _extract_scores(output, criteria_names):
    if not output or not output.message or not output.message.tool_calls:
        text = output.completion[:200] if output and output.completion else "(empty)"
        return None, None, f"No tool call. Response: {text}"

    args = {}
    for tc in output.message.tool_calls:
        if tc.function == "submit_scores":
            args.update(tc.arguments)

    if not args:
        return None, None, f"No submit_scores tool call found"

    missing = [n for n in criteria_names if n not in args]
    if missing:
        return None, None, f"Missing criteria: {missing}. Got: {list(args.keys())}"

    scores = {n: int(bool(args[n])) for n in criteria_names}
    return scores, args.get("reason", ""), None


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

                scores, reason, err = _extract_scores(result, criteria_names)
                if scores is not None:
                    for n in criteria_names:
                        votes[n].append(scores[n])
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
                results.append({"name": n, "votes_for": vf, "total": len(v), "score": vf / len(v)})

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


AGENT_TASK_DEFINITION = '''
@task
def eval_task():
    return Task(
        dataset=json_dataset(DATASET_PATH, FieldSpec(input="question", target="golden_answer")),
        solver=agent_solver(),
        scorer={scorer_expr},
        sandbox=("docker", "compose.yaml"),
    )
'''


AGENT_COMPOSE_TEMPLATE = """services:
  default:
    image: {image}
    command: tail -f /dev/null
"""


def _build_judge_system_prompt(criteria: List[Dict[str, str]]) -> str:
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


def create_agent_eval_files(
    dataset_path: str,
    config_name: str,
    config_dir: str,
    judge_config: JudgeConfig,
    agent_image: str,
    agent_cmd: List[str],
    model: str,
    description: Optional[str] = None,
    scorers: Optional[List[str]] = None,
) -> tuple[str, dict, str]:
    """Create agent task file, config JSON, and compose.yaml.

    Returns:
        (task_code, config_dict, compose_yaml)
    """
    scorers = _validate_scorers(scorers)

    config_data = {
        "dataset_path": dataset_path,
        "model": model,
        "judge_models": dict(judge_config.judges),
        "criteria": judge_config.criteria,
        "system_prompt": _build_judge_system_prompt(judge_config.criteria),
        "agent_image": agent_image,
        "agent_cmd": agent_cmd,
        "description": description or "",
        "scorers": scorers,
    }

    extra_imports = _render_builtin_scorer_imports(scorers)
    scorer_expr = _render_scorer_expression(scorers)

    parts: List[str] = []
    parts.append(AGENT_TASK_BASE.format(
        config_name=config_name,
        scorers_doc=", ".join(scorers),
        extra_imports=(extra_imports + "\n") if extra_imports else "",
    ))
    if "jury" in scorers:
        parts.append(AGENT_JURY_BLOCK)
    parts.append(AGENT_TASK_DEFINITION.format(scorer_expr=scorer_expr))
    task_code = "".join(parts)
    compose_yaml = AGENT_COMPOSE_TEMPLATE.format(image=agent_image)

    return task_code, config_data, compose_yaml


async def handle_create_agent_eval_config(args: Dict[str, Any]) -> List[TextContent]:
    """Handle create_agent_eval_config tool call."""
    try:
        import time
        dataset_name = args.get("dataset")
        judge_name = args.get("judge")
        # Auto-generated name — see note in create_eval_config.
        config_name = f"agent_eval_{int(time.time() * 1000)}"
        agent_image = args.get("agentImage")
        agent_cmd = args.get("agentCmd", ["python", "agent.py"])
        model = args.get("model")
        description = args.get("description")
        user_id = args.get("user_id")
        scorers_arg = args.get("scorers")

        if not user_id:
            return [TextContent(type="text", text=json.dumps({"success": False, "error": "user_id is required"}))]
        if not dataset_name:
            return [TextContent(type="text", text=json.dumps({"success": False, "error": "dataset is required"}))]
        if not judge_name:
            return [TextContent(type="text", text=json.dumps({"success": False, "error": "judge is required"}))]
        if not agent_image:
            return [TextContent(type="text", text=json.dumps({"success": False, "error": "agentImage is required"}))]

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

        inspect_samples = []
        for test in tests:
            v = test.get("vars", test)
            inspect_samples.append({
                "question": v.get("question", ""),
                "golden_answer": v.get("golden_answer", ""),
            })

        with open(dataset_file, "w") as f:
            json.dump(inspect_samples, f, indent=2)

        # Generate task file + config JSON + compose.yaml
        config_dir = user_dir / "configs"
        config_dir.mkdir(parents=True, exist_ok=True)

        task_code, config_data, compose_yaml = create_agent_eval_files(
            dataset_path=str(dataset_file),
            config_name=config_name,
            config_dir=str(config_dir),
            judge_config=judge_config,
            agent_image=agent_image,
            agent_cmd=agent_cmd,
            model=model,
            description=description,
            scorers=scorers,
        )

        # Write all files
        (config_dir / f"{config_name}.py").write_text(task_code)
        (config_dir / f"{config_name}.json").write_text(json.dumps(config_data, indent=2))
        (config_dir / "compose.yaml").write_text(compose_yaml)

        result = {
            "success": True,
            "configName": config_name,
            "summary": {
                "dataset": dataset_name,
                "judge": judge_name,
                "agentImage": agent_image,
                "agentCmd": agent_cmd,
                "model": model,
                "testCases": len(tests),
                "judges": list(judge_config.judges.keys()),
                "criteria": [c["name"] for c in criteria],
                "scorers": scorers,
                "description": description or f"Agent evaluation: {config_name}",
            },
            "nextStep": f"Run evaluation: run_evaluation(configName='{config_name}')",
        }

        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    except Exception as e:
        return [TextContent(type="text", text=json.dumps({"success": False, "error": f"Failed to create agent eval config: {str(e)}"}))]
