"""End-to-end proof: a LangChain agent (in its own venv) evaluated against
real Bedrock, with telemetry flowing back via the subprocess+OTLP path —
no dep skew, no `uv pip install langchain` into the harness venv.

This is the regression test for the exact failure that motivated the whole
subprocess-isolation work. Before the fix, evaluating examples/langchain_bedrock_agent
through the in-process bedrock_capture path required langchain to be installed
in the harness venv, which bumped boto3 and broke the OTel/botocore signature
the judges depended on (0/0 votes everywhere). With subprocess mode, the agent
runs in its own ephemeral venv built from its requirements.txt; nothing it
imports touches the harness.

Skipped automatically when AWS creds aren't reachable.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


def _aws_creds_available() -> bool:
    try:
        import boto3
        s = boto3.Session()
        return s.get_credentials() is not None and s.region_name is not None
    except Exception:
        return False


_REPO_ROOT = Path(__file__).resolve().parents[1]
_AGENT_DIR = _REPO_ROOT / "examples" / "langchain_bedrock_agent"


@pytest.mark.skipif(
    not _aws_creds_available(),
    reason="No AWS credentials / region — Bedrock E2E test skipped.",
)
@pytest.mark.skipif(
    not (_AGENT_DIR / "agent.py").exists()
    or not (_AGENT_DIR / "requirements.txt").exists(),
    reason="examples/langchain_bedrock_agent fixture is missing.",
)
def test_langchain_agent_runs_in_subprocess_with_otlp_capture(tmp_path: Path):
    """The full subprocess+OTLP path against a real LangChain agent.

    Builds an Inspect AI task via create_local_pipeline_eval_files (which
    auto-routes to subprocess mode because the agent dir ships a
    requirements.txt), runs it against a 1-sample math dataset, and asserts:

      - the eval log has a sample whose model output matches the golden
        answer (the agent works inside its uv-isolated venv);
      - at least one ModelEvent was emitted into the transcript (the OTLP
        path actually shipped spans back, not just the final answer).
    """
    import json

    from eval_mcp.core.pipeline_stages import PipelineConfig, PipelineStage
    from eval_mcp.tools.analyze_agent_path import detect_agent_requirements
    from eval_mcp.tools.create_pipeline_eval_config import (
        create_local_pipeline_eval_files,
    )

    # 1) Routing: the agent dir has a requirements.txt → subprocess mode.
    requirements_path = detect_agent_requirements(str(_AGENT_DIR / "agent.py"))
    assert requirements_path is not None, "requirements.txt should be detected"

    # 2) Minimal dataset — one math question with a known answer.
    dataset_file = tmp_path / "ds.json"
    dataset_file.write_text(json.dumps([
        {
            "question": "What is (7 * 8) + 3? Reply with only the number.",
            "golden_answer": "59",
            "expected_tools": ["multiply", "add"],
            "expected_steps": "multiply(7,8) then add(56,3)",
            "difficulty": "simple",
        },
    ]))

    # 3) Pipeline with only the deterministic tool-selection scorer so this
    #    test doesn't depend on judge LLMs — we want to prove the agent's
    #    Bedrock calls get captured, not test the judges.
    pipeline = PipelineConfig(stages=[
        PipelineStage(
            name="tool_selection",
            display_name="Tool Selection",
            order=1,
            scorer_type="deterministic",
            check="expected_tools_called",
        ),
    ])

    task_code, config_data = create_local_pipeline_eval_files(
        dataset_path=str(dataset_file),
        config_name="lc_e2e",
        pipeline=pipeline,
        judge_models={},  # unused — we only run the deterministic scorer
        agent_path=str(_AGENT_DIR / "agent.py"),
        agent_entry="run_agent",
        requirements_path=requirements_path,
    )

    # The routing decision must have actually fired.
    assert "subprocess_runner" in task_code, "should have routed to subprocess mode"

    # 4) Override the _eval_mcp_path inserted by create_local_pipeline_eval_files
    #    so the generated `sys.path.insert(0, ...)` points at our repo root.
    config_data["_eval_mcp_path"] = str(_REPO_ROOT)

    # 5) Materialize task + config in tmp_path so inspect can load them.
    task_file = tmp_path / "lc_e2e.py"
    task_file.write_text(task_code)
    (tmp_path / "lc_e2e.json").write_text(json.dumps(config_data, indent=2))

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    # Inspect's task loader globs relative paths only, so run from tmp_path
    # and pass the task file by name.
    result = subprocess.run(
        [
            sys.executable, "-m", "inspect_ai",
            "eval", task_file.name,
            "--log-dir", str(log_dir),
            "--log-format", "json",
            "--display=none",
        ],
        cwd=str(tmp_path),
        env={**os.environ, "PYTHONPATH": str(_REPO_ROOT)},
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, (
        f"inspect eval failed (exit {result.returncode})\n"
        f"stderr:\n{result.stderr[-2000:]}"
    )

    # 6) Open the produced log and check both outcomes.
    logs = list(log_dir.glob("*.json"))
    assert logs, f"no eval log produced. stderr: {result.stderr[-1000:]}"
    log = json.loads(logs[0].read_text())

    samples = log.get("samples") or []
    assert len(samples) == 1, f"expected 1 sample, got {len(samples)}"
    s = samples[0]

    # Agent answered correctly — proves the langchain agent ran in its own
    # uv-managed venv successfully (the original integration failure was
    # the agent never producing valid output).
    output = (s.get("output") or {}).get("completion", "")
    assert "59" in str(output), f"agent answer should contain 59, got: {output!r}"

    # OTLP path delivered spans into the transcript — proves the subprocess
    # path captured Bedrock telemetry, not just the final stdout. We look
    # for ModelEvents in the events stream.
    events = s.get("events") or []
    model_events = [e for e in events if e.get("event") == "model"]
    assert model_events, (
        "no ModelEvent in transcript — OTLP capture failed. "
        f"events seen: {[e.get('event') for e in events]}"
    )
