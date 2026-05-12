"""Wire-up tests: analyze_agent_path → subprocess-mode task code.

The subprocess agent runner is built; these tests check the routing layer
that selects it. The contract:

  - When `requirements.txt` sits next to the agent file, the generated
    Inspect task code uses subprocess_runner.run_agent_subprocess (with the
    OTLP receiver running) instead of in-process bedrock_capture.
  - When there's no requirements.txt, the existing in-process path is
    preserved for backward compat with examples that don't ship one.
  - The agent venv's requirements path flows through the config JSON to
    the generated task at runtime.
"""

from __future__ import annotations

from pathlib import Path


def test_generate_pipeline_task_code_subprocess_mode_uses_runner():
    """`mode="subprocess"` produces task code that imports the subprocess
    runner and the OTLP receiver — not bedrock_capture's in-process patch.
    """
    from eval_mcp.core.pipeline_stages import PipelineConfig
    from eval_mcp.tools.create_pipeline_eval_config import (
        generate_pipeline_task_code,
    )

    pipeline = PipelineConfig.default_for_agent()
    code = generate_pipeline_task_code(
        config_name="t1",
        pipeline=pipeline,
        judge_models={"claude": "bedrock/us.anthropic.claude-sonnet-4-6"},
        mode="subprocess",
    )

    # Hard signals: the in-process monkeypatch context-manager must be absent,
    # the subprocess + OTLP receiver path must be present. The existing
    # _InspectSpanExporter / _InspectLogExporter classes live in
    # bedrock_capture and ARE reused (the conversion layer is shared) —
    # what we forbid is `with bedrock_capture():`, the in-process
    # monkeypatch entry point.
    assert "with bedrock_capture(" not in code, (
        "subprocess-mode task code must not enter the in-process "
        "bedrock_capture() context manager."
    )
    assert "subprocess_runner" in code
    assert "otlp_receiver" in code
    assert "run_agent_subprocess" in code
    # Receiver lifecycle must be explicit so each sample gets a fresh port
    # (no concurrent-sample collision).
    assert "start_receiver" in code


def test_create_local_pipeline_eval_files_routes_to_subprocess_when_reqs_given():
    """Passing requirements_path triggers subprocess-mode codegen. Without
    it, falls back to the existing in-process mode for backward compat
    with examples that don't ship a requirements file (e.g. local_agent).
    """
    from eval_mcp.core.pipeline_stages import PipelineConfig
    from eval_mcp.tools.create_pipeline_eval_config import (
        create_local_pipeline_eval_files,
    )

    pipeline = PipelineConfig.default_for_agent()

    sub_code, sub_cfg = create_local_pipeline_eval_files(
        dataset_path="/x/d.json",
        config_name="t-sub",
        pipeline=pipeline,
        judge_models={"claude": "bedrock/us.anthropic.claude-sonnet-4-6"},
        agent_path="/x/agent.py",
        agent_entry="run_agent",
        requirements_path="/x/requirements.txt",
    )
    assert "subprocess_runner" in sub_code
    assert "with bedrock_capture(" not in sub_code
    assert sub_cfg["requirements_path"] == "/x/requirements.txt"

    inproc_code, inproc_cfg = create_local_pipeline_eval_files(
        dataset_path="/x/d.json",
        config_name="t-inproc",
        pipeline=pipeline,
        judge_models={"claude": "bedrock/us.anthropic.claude-sonnet-4-6"},
        agent_path="/x/agent.py",
        agent_entry="run_agent",
    )
    # Legacy in-process path enters the bedrock_capture context manager and
    # does not invoke the subprocess runner.
    assert "with bedrock_capture(" in inproc_code
    assert "subprocess_runner" not in inproc_code
    # No requirements_path → key absent (kept lean rather than nulled).
    assert "requirements_path" not in inproc_cfg


def test_analyze_agent_path_detects_requirements_file(tmp_path: Path):
    """If `requirements.txt` sits next to the agent file, analyze_agent_path
    must pass that path through to the config so the generated task
    builds the right uv-isolated venv. This is the routing decision —
    no behavioral change for agents that don't ship a requirements file.
    """
    from eval_mcp.tools.analyze_agent_path import detect_agent_requirements

    agent_file = tmp_path / "agent.py"
    agent_file.write_text("def run_agent(p): return p\n")
    reqs_file = tmp_path / "requirements.txt"
    reqs_file.write_text("langchain\n")

    assert detect_agent_requirements(str(agent_file)) == str(reqs_file)


def test_analyze_agent_path_returns_none_when_no_requirements(tmp_path: Path):
    """No requirements.txt → returns None → caller falls back to in-process
    mode (preserves backward compat for examples that haven't migrated).
    """
    from eval_mcp.tools.analyze_agent_path import detect_agent_requirements

    agent_file = tmp_path / "agent.py"
    agent_file.write_text("def run_agent(p): return p\n")

    assert detect_agent_requirements(str(agent_file)) is None
