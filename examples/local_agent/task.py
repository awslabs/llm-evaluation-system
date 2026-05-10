"""Evaluate a local Python agent with full Bedrock call tracing.

No Docker. No code modification. Works with any agent calling Bedrock via boto3.

Usage:
    inspect eval examples/local_agent/task.py --model bedrock/us.anthropic.claude-sonnet-4-6
"""

import importlib.util
from pathlib import Path

from inspect_ai import Task, task
from inspect_ai.dataset import MemoryDataset, Sample
from inspect_ai.scorer import Score, accuracy, scorer, stderr
from inspect_ai.solver import Generate, TaskState, solver

# Add project root to path so eval_mcp is importable
import os
import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from eval_mcp.bedrock_capture import bedrock_capture

# Default logs to ~/.eval-mcp/users/local/logs so `eval-mcp view` finds them
if "INSPECT_LOG_DIR" not in os.environ:
    os.environ["INSPECT_LOG_DIR"] = str(Path.home() / ".eval-mcp" / "users" / "local" / "logs")

# Load agent
_agent_path = Path(__file__).parent / "agent.py"
_spec = importlib.util.spec_from_file_location("local_agent", _agent_path)
_agent_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_agent_module)
run_agent = _agent_module.run_agent

SAMPLES = [
    Sample(input="What is 25 * 4?", target="100"),
    Sample(input="What is machine learning?", target="Machine learning is a subset of artificial intelligence"),
    Sample(input="Calculate 144 / 12", target="12"),
    Sample(input="What is photosynthesis?", target="The process by which green plants convert sunlight"),
    Sample(input="What is 7 squared plus 3?", target="52"),
]


@solver
def agent_solver():
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        with bedrock_capture():
            result = run_agent(state.input_text)
        state.output.completion = result
        return state
    return solve


@scorer(metrics=[accuracy(), stderr()])
def correctness_scorer():
    async def score(state: TaskState, target) -> Score:
        output = state.output.completion.lower() if state.output else ""
        expected = target.text.lower() if target else ""

        if not output:
            return Score(value="I", explanation="No output from agent")

        try:
            out_num = float(output.strip().rstrip('.'))
            exp_num = float(expected.strip().rstrip('.'))
            if abs(out_num - exp_num) < 0.01:
                return Score(value="C", explanation="Correct numeric answer")
        except (ValueError, TypeError):
            pass

        if len(expected) > 20:
            key_words = [w for w in expected.split() if len(w) > 4]
            matches = sum(1 for w in key_words if w in output)
            ratio = matches / len(key_words) if key_words else 0
            if ratio > 0.4:
                return Score(value="C", explanation=f"Contains key concepts ({ratio:.0%} match)")

        if expected in output:
            return Score(value="C", explanation="Contains expected answer")

        return Score(value="I", explanation=f"Expected: {expected[:100]}. Got: {output[:100]}")

    return score


@task
def eval_local_agent():
    return Task(
        dataset=MemoryDataset(samples=SAMPLES),
        solver=agent_solver(),
        scorer=correctness_scorer(),
    )
