"""Create Inspect AI pipeline-based agent evaluation task files.

Generates task files with multiple scorers — one per pipeline stage.
Each stage evaluates a different aspect of agent behavior:
- Deterministic stages: simple code checks (was tool X called?)
- LLM judge stages: model-graded scoring with focused context
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp.types import TextContent

from eval_mcp.core.pipeline_stages import PipelineConfig, PipelineStage
from eval_mcp.core.user_storage import get_user_dir


def _generate_deterministic_scorer(stage: PipelineStage) -> str:
    """Generate code for a deterministic scorer function."""
    if stage.check == "tool_called":
        return f'''
@scorer(metrics=[accuracy(), stderr()])
def stage_{stage.name}():
    """Deterministic: check if expected tools were called."""
    async def score(state, target):
        from inspect_ai.log._samples import sample_active

        # Read ALL tool calls from transcript events (captures multi-agent traces)
        tool_names_called = set()
        sample = sample_active()
        if sample and sample.transcript:
            for ev in sample.transcript.events:
                if type(ev).__name__ == "ModelEvent" and hasattr(ev, "output") and ev.output:
                    msg = ev.output.message
                    if hasattr(msg, "tool_calls") and msg.tool_calls:
                        for tc in msg.tool_calls:
                            tool_names_called.add(tc.function)

        # Exclude judge tool calls (submit_scores)
        tool_names_called.discard("submit_scores")

        expected = set(state.metadata.get("{stage.expected_field}", []))
        if not expected:
            return Score(value="C", explanation="No expected tools specified", metadata={{"stage": "{stage.name}", "stage_order": {stage.order}}})

        matched = expected.issubset(tool_names_called)
        return Score(
            value="C" if matched else "I",
            explanation=f"Called: {{sorted(tool_names_called)}}. Expected: {{sorted(expected)}}",
            metadata={{"stage": "{stage.name}", "stage_order": {stage.order}, "tools_called": sorted(tool_names_called), "tools_expected": sorted(expected)}},
        )
    return score
'''
    elif stage.check == "includes_text":
        return f'''
@scorer(metrics=[accuracy(), stderr()])
def stage_{stage.name}():
    """Deterministic: check if output includes expected text."""
    async def score(state, target):
        output = state.output.completion if state.output else ""
        expected = target.text if target else ""
        found = expected.lower() in output.lower() if expected else True
        return Score(
            value="C" if found else "I",
            explanation=f"Expected '{{expected[:50]}}' in output: {{'found' if found else 'not found'}}",
            metadata={{"stage": "{stage.name}", "stage_order": {stage.order}}},
        )
    return score
'''
    return f'''
@scorer(metrics=[accuracy(), stderr()])
def stage_{stage.name}():
    """Deterministic: {stage.check}."""
    async def score(state, target):
        return Score(value="C", explanation="Check not implemented: {stage.check}", metadata={{"stage": "{stage.name}", "stage_order": {stage.order}}})
    return score
'''


def _generate_llm_judge_scorer(stage: PipelineStage, judge_models: Dict[str, str]) -> str:
    """Generate code for an LLM judge scorer function."""
    criteria = stage.criteria or []
    criteria_json = json.dumps(criteria)
    judges_json = json.dumps(judge_models)

    context_extractor = ""
    if stage.context_filter == "final_output":
        context_extractor = '''
        output = state.output.completion if state.output else ""
        context = f"Agent output:\\n{output}"'''
    elif stage.context_filter == "tool_calls_only":
        context_extractor = '''
        from inspect_ai.log._samples import sample_active
        tool_parts = []
        sample = sample_active()
        if sample and sample.transcript:
            for ev in sample.transcript.events:
                if type(ev).__name__ == "ModelEvent" and getattr(ev, "output", None):
                    msg = ev.output.message
                    if getattr(msg, "tool_calls", None):
                        for tc in msg.tool_calls:
                            if tc.function == "submit_scores":
                                continue
                            tool_parts.append(f"Called {tc.function}({json.dumps(tc.arguments)})")
        context = "Tool calls made:\\n" + "\\n".join(tool_parts) if tool_parts else "No tool calls"'''
    elif stage.context_filter == "first_response":
        context_extractor = '''
        from inspect_ai.log._samples import sample_active
        first_response = ""
        sample = sample_active()
        if sample and sample.transcript:
            for ev in sample.transcript.events:
                if type(ev).__name__ == "ModelEvent" and getattr(ev, "output", None):
                    msg = ev.output.message
                    text = getattr(msg, "text", None)
                    if text is None:
                        content = getattr(msg, "content", "")
                        text = content if isinstance(content, str) else str(content)
                    if text:
                        first_response = text
                        break
        context = f"First response:\\n{first_response}"'''
    elif stage.context_filter == "all":
        context_extractor = '''
        from inspect_ai.log._samples import sample_active
        steps = []
        sample = sample_active()
        if sample and sample.transcript:
            for ev in sample.transcript.events:
                if type(ev).__name__ != "ModelEvent" or not getattr(ev, "output", None):
                    continue
                msg = ev.output.message
                text = getattr(msg, "text", None)
                if text is None:
                    content = getattr(msg, "content", "")
                    text = content if isinstance(content, str) else str(content)
                line = f"[{getattr(ev, 'model', 'model')}]"
                if text:
                    line += f" {text}"
                if getattr(msg, "tool_calls", None):
                    for tc in msg.tool_calls:
                        if tc.function == "submit_scores":
                            continue
                        line += f"\\n  -> {tc.function}({json.dumps(tc.arguments)})"
                steps.append(line)
        output = state.output.completion if state.output else ""
        context = "Trajectory:\\n" + "\\n".join(steps) + f"\\n\\nFinal output:\\n{output}" if steps else f"Agent output:\\n{output}"'''
    else:
        context_extractor = '''
        output = state.output.completion if state.output else ""
        context = f"Agent output:\\n{output}"'''

    return f'''
@scorer(metrics=[accuracy(), stderr()])
def stage_{stage.name}():
    """LLM judge: {stage.display_name}."""
    _stage_criteria = {criteria_json}
    _stage_judges = {judges_json}

    def _build_stage_tool():
        properties = {{}}
        required = []
        for c in _stage_criteria:
            properties[c["name"]] = {{"type": "integer", "description": f"Score for {{c['name']}}: 1 if pass, 0 if fail", "enum": [0, 1]}}
            required.append(c["name"])
        properties["reason"] = {{"type": "string", "description": "Brief explanation"}}
        required.append("reason")
        return ToolInfo(name="submit_scores", description="Submit binary scores", parameters=ToolParams(type="object", properties=properties, required=required))

    async def score(state, target):
        {context_extractor}

        question = str(state.input)
        golden = target.text if target else ""
        criteria_names = [c["name"] for c in _stage_criteria]
        tool = _build_stage_tool()

        system_prompt = "You are a judge evaluating an AI agent. Score each criterion as 1 (pass) or 0 (fail), then call submit_scores.\\n\\nCriteria:\\n" + "\\n".join([f"- {{c['name']}}: {{c['description']}}" for c in _stage_criteria])

        votes = {{n: [] for n in criteria_names}}
        details = []
        errors = []

        for label, model_id in _stage_judges.items():
            try:
                judge = get_model(model_id)
                judge_msgs = [
                    ChatMessageSystem(content=system_prompt),
                    ChatMessageUser(content=f"Question:\\n{{question}}\\n\\n{{context}}\\n\\nReference Answer:\\n{{golden}}"),
                ]
                args = {{}}
                for attempt in range(2):
                    result = await judge.generate(judge_msgs, tools=[tool], tool_choice="any")
                    if result.message and result.message.tool_calls:
                        for tc in result.message.tool_calls:
                            if tc.function == "submit_scores":
                                args.update(tc.arguments)
                    if args:
                        break
                if args:
                    for n in criteria_names:
                        if n in args:
                            votes[n].append(int(bool(args[n])))
                    details.append(f"  {{label}}: {{{{n: args.get(n) for n in criteria_names}}}} - {{args.get('reason', '')}}")
                else:
                    errors.append(f"  {{label}}: No submit_scores call after retry")
            except Exception as e:
                errors.append(f"  {{label}}: {{str(e)[:100]}}")

        results = []
        for n in criteria_names:
            v = votes[n]
            if not v:
                results.append({{"name": n, "votes_for": 0, "total": 0, "passed": False}})
            else:
                vf = sum(v)
                results.append({{"name": n, "votes_for": vf, "total": len(v), "passed": vf > len(v) / 2}})

        n_passed = sum(1 for r in results if r["passed"])
        n_total = len(criteria_names)
        passed = n_passed > n_total / 2 if n_total > 0 else True

        lines = [f"Stage {stage.display_name}: {{'PASS' if passed else 'FAIL'}} ({{n_passed}}/{{n_total}})"]
        for r in results:
            s = "PASS" if r["passed"] else "FAIL"
            lines.append(f"  {{r['name']}}: {{s}} ({{r['votes_for']}}/{{r['total']}})")
        if details:
            lines += ["", "Judges:"] + details
        if errors:
            lines += ["", "Errors:"] + errors

        return Score(
            value="C" if passed else "I",
            answer=(state.output.completion if state.output else "")[:200],
            explanation="\\n".join(lines),
            metadata={{"stage": "{stage.name}", "stage_order": {stage.order}, "criteria_results": results}},
        )
    return score
'''


def generate_pipeline_task_code(
    config_name: str,
    pipeline: PipelineConfig,
    judge_models: Dict[str, str],
    mode: str = "container",
) -> str:
    """Generate the full task file code with multi-stage scorers.

    Args:
        mode: "container" runs the agent in a Docker/k8s sandbox via
            sandbox_agent_bridge (intercepts OpenAI/Anthropic SDK calls).
            "local" runs the agent in-process via importlib and captures
            Bedrock calls via OpenTelemetry (eval_mcp.bedrock_capture).
    """

    scorer_functions = []
    scorer_names = []
    for stage in sorted(pipeline.stages, key=lambda s: s.order):
        if stage.scorer_type == "deterministic":
            scorer_functions.append(_generate_deterministic_scorer(stage))
        else:
            scorer_functions.append(_generate_llm_judge_scorer(stage, judge_models))
        scorer_names.append(f"stage_{stage.name}()")

    scorers_list = ", ".join(scorer_names)

    if mode == "subprocess":
        return _generate_subprocess_task_code(config_name, scorer_functions, scorers_list)
    if mode == "local":
        return _generate_local_task_code(config_name, scorer_functions, scorers_list)
    return _generate_container_task_code(config_name, scorer_functions, scorers_list)


def _generate_container_task_code(config_name: str, scorer_functions: list, scorers_list: str) -> str:
    return f'''"""Inspect AI pipeline agent evaluation: {config_name}

Auto-generated. Multi-stage evaluation with separate scorers per stage.
Container mode: runs agent in Docker/k8s with sandbox_agent_bridge.
"""

import json
from pathlib import Path

from inspect_ai import Task, task
from inspect_ai.agent import Agent, AgentState, agent, sandbox_agent_bridge
from inspect_ai.dataset import json_dataset, FieldSpec
from inspect_ai.model import ChatMessageUser, ChatMessageSystem, get_model
from inspect_ai.scorer import Score, accuracy, scorer, stderr
from inspect_ai.util import sandbox

from inspect_ai.tool._tool_info import ToolInfo
from inspect_ai.tool._tool_params import ToolParams

_config_path = Path(__file__).with_suffix(".json")
CONFIG = json.loads(_config_path.read_text())

DATASET_PATH = CONFIG["dataset_path"]
AGENT_CMD = CONFIG["agent_cmd"]


@agent
def agent_solver() -> Agent:
    """Run the user's agent in a container with LLM interception."""

    async def execute(state: AgentState) -> AgentState:
        async with sandbox_agent_bridge(state) as bridge:
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

{"".join(scorer_functions)}

@task
def eval_task():
    import os
    sandbox_type = os.environ.get("INSPECT_SANDBOX_TYPE", "docker")
    if sandbox_type == "k8s":
        sandbox_config = ("k8s", "values.yaml")
    else:
        sandbox_config = ("docker", "compose.yaml")

    return Task(
        dataset=json_dataset(DATASET_PATH, FieldSpec(input="question", target="golden_answer", metadata=["expected_tools", "expected_steps", "difficulty"])),
        solver=agent_solver(),
        scorer=[{scorers_list}],
        sandbox=sandbox_config,
    )
'''


def _generate_local_task_code(config_name: str, scorer_functions: list, scorers_list: str) -> str:
    return f'''"""Inspect AI pipeline agent evaluation: {config_name}

Auto-generated. Multi-stage evaluation with separate scorers per stage.
Local mode: runs agent in-process with bedrock_capture (OpenTelemetry).
"""

import importlib.util
import json
import sys
from pathlib import Path

from inspect_ai import Task, task
from inspect_ai.dataset import json_dataset, FieldSpec
from inspect_ai.model import ChatMessageUser, ChatMessageSystem, get_model
from inspect_ai.scorer import Score, accuracy, scorer, stderr
from inspect_ai.solver import Generate, TaskState, solver

from inspect_ai.tool._tool_info import ToolInfo
from inspect_ai.tool._tool_params import ToolParams

_config_path = Path(__file__).with_suffix(".json")
CONFIG = json.loads(_config_path.read_text())

DATASET_PATH = CONFIG["dataset_path"]

sys.path.insert(0, CONFIG.get("_eval_mcp_path", ""))
from eval_mcp.bedrock_capture import bedrock_capture

_agent_spec = importlib.util.spec_from_file_location("_user_agent", CONFIG["agent_path"])
_agent_module = importlib.util.module_from_spec(_agent_spec)
_agent_spec.loader.exec_module(_agent_module)
_agent_fn = getattr(_agent_module, CONFIG.get("agent_entry", "run_agent"))


@solver
def agent_solver():
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        with bedrock_capture():
            result = _agent_fn(state.input_text)
        state.output.completion = str(result)
        return state
    return solve

{"".join(scorer_functions)}

@task
def eval_task():
    return Task(
        dataset=json_dataset(DATASET_PATH, FieldSpec(input="question", target="golden_answer", metadata=["expected_tools", "expected_steps", "difficulty"])),
        solver=agent_solver(),
        scorer=[{scorers_list}],
    )
'''


def _generate_subprocess_task_code(config_name: str, scorer_functions: list, scorers_list: str) -> str:
    """Generate task code for the subprocess-isolated agent path.

    Each sample spins up its own OTLP receiver and spawns the agent in an
    ephemeral uv-managed venv. Spans flow over OTLP to the receiver, which
    feeds them through the existing _InspectSpanExporter / _InspectLogExporter
    so the transcript ends up identical to in-process mode — downstream
    scorers can't tell the difference.
    """
    return f'''"""Inspect AI pipeline agent evaluation: {config_name}

Auto-generated. Multi-stage evaluation with separate scorers per stage.
Subprocess mode: each sample spawns the agent in an isolated uv-managed
venv, with telemetry flowing back via in-harness OTLP.
"""

import json
import sys
from pathlib import Path

from inspect_ai import Task, task
from inspect_ai.dataset import json_dataset, FieldSpec
from inspect_ai.model import ChatMessageUser, ChatMessageSystem, get_model
from inspect_ai.scorer import Score, accuracy, scorer, stderr
from inspect_ai.solver import Generate, TaskState, solver

from inspect_ai.tool._tool_info import ToolInfo
from inspect_ai.tool._tool_params import ToolParams

_config_path = Path(__file__).with_suffix(".json")
CONFIG = json.loads(_config_path.read_text())

DATASET_PATH = CONFIG["dataset_path"]

sys.path.insert(0, CONFIG.get("_eval_mcp_path", ""))
from eval_mcp.bedrock_capture import _InspectLogExporter, _InspectSpanExporter
from eval_mcp.otlp_receiver import start_receiver
from eval_mcp.subprocess_runner import run_agent_subprocess


@solver
def agent_solver():
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        # Per-sample receiver: each sample gets its own ephemeral port so
        # spans never leak across concurrently-running samples. Buffer the
        # received batches; drain them through the exporters AFTER the
        # agent subprocess exits, in this coroutine — that's the only
        # place where transcript() (an Inspect AI contextvar) resolves to
        # this sample's transcript. (The receiver runs in a separate
        # thread which can't see the contextvar.)
        handle = start_receiver()
        try:
            result = run_agent_subprocess(
                agent_path=CONFIG["agent_path"],
                agent_entry=CONFIG.get("agent_entry", "run_agent"),
                prompt=state.input_text,
                otlp_endpoint=handle.url,
                sample_id=str(getattr(state, "sample_id", state.input_text[:32])),
                requirements_path=CONFIG.get("requirements_path"),
                venv_python=CONFIG.get("venv_python"),
                timeout=300,
            )
            state.output.completion = str(result)

            # Drain in the sample's coroutine so transcript() points here.
            # Logs are processed first because they emit ModelEvents that
            # spans then enrich with model name + token counts.
            spans, logs = handle.drain()
            log_exp = _InspectLogExporter()
            span_exp = _InspectSpanExporter(log_exp)
            if logs:
                log_exp.export(logs)
            if spans:
                span_exp.export(spans)
        finally:
            handle.shutdown()
        return state
    return solve

{"".join(scorer_functions)}

@task
def eval_task():
    return Task(
        dataset=json_dataset(DATASET_PATH, FieldSpec(input="question", target="golden_answer", metadata=["expected_tools", "expected_steps", "difficulty"])),
        solver=agent_solver(),
        scorer=[{scorers_list}],
    )
'''


AGENT_COMPOSE_TEMPLATE = """services:
  default:
    image: {image}
    command: tail -f /dev/null
"""

AGENT_K8S_VALUES_TEMPLATE = """services:
  default:
    image: {image}
    command: ["tail", "-f", "/dev/null"]
    runtimeClassName: gvisor
    resources:
      requests:
        memory: "512Mi"
        cpu: "250m"
"""


def create_pipeline_eval_files(
    dataset_path: str,
    config_name: str,
    config_dir: str,
    pipeline: PipelineConfig,
    judge_models: Dict[str, str],
    agent_image: str,
    agent_cmd: List[str],
    model: str,
    description: Optional[str] = None,
) -> tuple[str, dict, str, str]:
    """Create pipeline task file, config JSON, compose.yaml, and values.yaml.

    Returns:
        (task_code, config_dict, compose_yaml, k8s_values_yaml)
    """
    task_code = generate_pipeline_task_code(config_name, pipeline, judge_models, mode="container")
    compose_yaml = AGENT_COMPOSE_TEMPLATE.format(image=agent_image)
    k8s_values = AGENT_K8S_VALUES_TEMPLATE.format(image=agent_image)

    config_data = {
        "dataset_path": dataset_path,
        "model": model,
        "judge_models": judge_models,
        "pipeline_stages": pipeline.to_dict(),
        "agent_image": agent_image,
        "agent_cmd": agent_cmd,
        "description": description or "",
    }

    return task_code, config_data, compose_yaml, k8s_values


def create_local_pipeline_eval_files(
    dataset_path: str,
    config_name: str,
    pipeline: PipelineConfig,
    judge_models: Dict[str, str],
    agent_path: str,
    agent_entry: str = "run_agent",
    description: Optional[str] = None,
    requirements_path: Optional[str] = None,
    venv_python: Optional[str] = None,
) -> tuple[str, dict]:
    """Create pipeline task file + config JSON for non-container evaluation.

    Three modes, chosen by which extra args are supplied:

      subprocess + user-venv (preferred when their setup works):
          venv_python set. We spawn via their existing venv's
          opentelemetry-instrument. Their environment is read-only to us.

      subprocess + managed-mirror (when only requirements_path is given):
          We build an ephemeral uv venv from their requirements.txt plus
          our OTel deps. Their actual env is untouched.

      local (legacy, neither is given):
          Agent loaded via importlib in the harness process; Bedrock calls
          captured via in-process OTel monkeypatch. Kept for backward
          compat with examples that haven't migrated.

    Returns: (task_code, config_dict)
    """
    mode = "subprocess" if (venv_python or requirements_path) else "local"
    task_code = generate_pipeline_task_code(config_name, pipeline, judge_models, mode=mode)
    eval_mcp_src = str(Path(__file__).parent.parent.parent.parent.parent / "eval_mcp" / "src")

    config_data = {
        "dataset_path": dataset_path,
        "judge_models": judge_models,
        "pipeline_stages": pipeline.to_dict(),
        "agent_path": agent_path,
        "agent_entry": agent_entry,
        "_eval_mcp_path": eval_mcp_src,
        "description": description or "",
    }
    if requirements_path:
        config_data["requirements_path"] = requirements_path
    if venv_python:
        config_data["venv_python"] = venv_python
    return task_code, config_data
