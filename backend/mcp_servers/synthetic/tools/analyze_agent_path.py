"""Analyze a local Python agent and generate a comprehensive evaluation dataset.

Same pipeline as analyze_agent_image, but reads the agent code from a local
path instead of pulling from a container image. The agent runs in-process
during evaluation with bedrock_capture (OpenTelemetry) instead of in a
sandboxed container.
"""

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp.types import TextContent

from backend.core.bedrock_client import BedrockClient
from backend.core.user_storage import get_user_dir, save_dataset_to_db
from backend.mcp_servers.synthetic.tools.analyze_agent_image import (
    AGENT_DEEP_ANALYSIS_TOOL,
    analyze_agent_deep,
)

logger = logging.getLogger(__name__)


def read_agent_code(agent_path: str) -> Dict[str, str]:
    """Read the user's agent file plus any sibling .py files in the same dir.

    Mirrors what extract_code_from_image returns: {relative_path: source}.
    """
    p = Path(agent_path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f"Agent path not found: {agent_path}")
    if not p.is_file():
        raise ValueError(f"agent_path must be a Python file, got: {agent_path}")

    files: Dict[str, str] = {p.name: p.read_text(encoding="utf-8", errors="replace")}

    # Pull in sibling .py files from the same directory (helps Claude understand
    # tools / sub-agents defined in adjacent modules) but skip large files.
    parent = p.parent
    for sibling in parent.glob("*.py"):
        if sibling == p or sibling.name.startswith("test_") or sibling.name == "__init__.py":
            continue
        try:
            content = sibling.read_text(encoding="utf-8", errors="replace")
            if len(content) < 50000:
                files[sibling.name] = content
        except Exception:
            continue

    return files


async def handle_analyze_agent_path(args: Dict[str, Any]) -> List[TextContent]:
    """Read the agent file, analyze it, generate dataset + pipeline, write config."""
    from backend.core.pipeline_stages import PipelineConfig, PipelineStage
    from backend.core.judge_config import JUDGE_MODELS
    from backend.mcp_servers.synthetic.tools.create_pipeline_eval_config import (
        create_local_pipeline_eval_files,
    )

    try:
        agent_path = args.get("agentPath")
        agent_entry = args.get("agentEntry", "run_agent")
        user_id = args.get("user_id")
        num_samples = args.get("numSamples", 15)
        config_name = args.get("configName", "agent_evaluation")
        user_context = args.get("context")

        if not user_id:
            return [TextContent(type="text", text=json.dumps({"success": False, "error": "user_id is required"}))]
        if not agent_path:
            return [TextContent(type="text", text=json.dumps({"success": False, "error": "agentPath is required"}))]

        try:
            code_files = read_agent_code(agent_path)
        except Exception as e:
            return [TextContent(type="text", text=json.dumps({"success": False, "error": f"Failed to read agent: {str(e)}"}))]

        bedrock = BedrockClient(region=os.environ.get("AWS_REGION", "us-west-2"))
        analysis = await analyze_agent_deep(bedrock, code_files, num_samples, user_context)

        test_cases = analysis.get("test_cases", [])
        if not test_cases:
            return [TextContent(type="text", text=json.dumps({"success": False, "error": "Could not generate test cases from agent code"}))]

        user_dir = get_user_dir(user_id)
        temp_dir = user_dir / "temp"
        temp_dir.mkdir(parents=True, exist_ok=True)

        dataset_name = f"agent_{config_name}"
        dataset_file = temp_dir / f"{dataset_name}.json"

        inspect_samples = []
        for tc in test_cases:
            if not tc.get("question"):
                continue
            inspect_samples.append({
                "question": tc["question"],
                "golden_answer": tc["golden_answer"],
                "expected_tools": tc.get("expected_tools", []),
                "expected_steps": tc.get("expected_steps", ""),
                "difficulty": tc.get("difficulty", "moderate"),
            })

        with open(dataset_file, "w") as f:
            json.dump(inspect_samples, f, indent=2)

        db_tests = [{"vars": s} for s in inspect_samples]
        save_dataset_to_db(user_id, dataset_name, db_tests)

        raw_stages = analysis.get("pipeline_stages", [])
        if raw_stages:
            stages = []
            for s in raw_stages:
                stages.append(PipelineStage(
                    name=s["name"],
                    display_name=s["display_name"],
                    order=s["order"],
                    scorer_type=s["scorer_type"],
                    criteria=s.get("criteria"),
                    check=s.get("check"),
                    expected_field=s.get("expected_field"),
                    context_filter=s.get("context_filter", "all"),
                ))
            pipeline = PipelineConfig(stages=stages)
        else:
            pipeline = PipelineConfig.default_for_agent()

        config_dir = user_dir / "configs"
        config_dir.mkdir(parents=True, exist_ok=True)

        task_code, config_data = create_local_pipeline_eval_files(
            dataset_path=str(dataset_file),
            config_name=config_name,
            pipeline=pipeline,
            judge_models=JUDGE_MODELS,
            agent_path=str(Path(agent_path).expanduser().resolve()),
            agent_entry=agent_entry,
        )

        (config_dir / f"{config_name}.py").write_text(task_code)
        (config_dir / f"{config_name}.json").write_text(json.dumps(config_data, indent=2))

        result = {
            "success": True,
            "configName": config_name,
            "summary": {
                "agent_summary": analysis.get("agent_summary", ""),
                "framework": analysis.get("framework", "unknown"),
                "tools_found": [t["name"] for t in analysis.get("tools", [])],
                "subagents": [s["name"] for s in analysis.get("subagents", [])],
                "test_cases": len(inspect_samples),
                "pipeline_stages": [s.display_name for s in pipeline.stages],
                "difficulty_breakdown": {
                    "simple": sum(1 for tc in test_cases if tc.get("difficulty") == "simple"),
                    "moderate": sum(1 for tc in test_cases if tc.get("difficulty") == "moderate"),
                    "complex": sum(1 for tc in test_cases if tc.get("difficulty") == "complex"),
                },
                "agentPath": agent_path,
                "agentEntry": agent_entry,
            },
            "nextStep": f"Run evaluation: run_evaluation(configName='{config_name}')",
        }

        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    except Exception as e:
        logger.exception("Failed to analyze agent path")
        return [TextContent(type="text", text=json.dumps({"success": False, "error": f"Failed to analyze agent: {str(e)}"}))]
