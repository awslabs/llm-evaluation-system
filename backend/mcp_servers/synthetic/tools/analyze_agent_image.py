"""Analyze an agent container image and generate comprehensive evaluation dataset.

Extracts code from the image, analyzes tools/subagents/logic, and generates
test cases that cover output correctness, tool usage, and trajectory.
"""

import asyncio
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp.types import TextContent

from backend.core.bedrock_client import BedrockClient
from backend.core.user_storage import get_user_dir, save_dataset_to_db

logger = logging.getLogger(__name__)


AGENT_DEEP_ANALYSIS_TOOL = {
    "name": "submit_agent_evaluation_plan",
    "description": "Submit the complete agent analysis with rich test cases covering output, tool usage, and trajectory.",
    "input_schema": {
        "type": "object",
        "properties": {
            "agent_summary": {
                "type": "string",
                "description": "One paragraph summary of what this agent does",
            },
            "framework": {
                "type": "string",
                "enum": ["strands", "crewai", "langgraph", "openai", "anthropic", "custom"],
                "description": "Agent framework detected",
            },
            "tools": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "parameters": {"type": "string", "description": "Key parameters as comma-separated list"},
                    },
                    "required": ["name", "description"],
                },
                "description": "All tools/functions the agent can call",
            },
            "subagents": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "role": {"type": "string"},
                    },
                    "required": ["name", "role"],
                },
                "description": "Any sub-agents or delegated agents",
            },
            "test_cases": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string", "description": "Input prompt to the agent"},
                        "golden_answer": {"type": "string", "description": "Expected final output (or key content)"},
                        "expected_tools": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Tools that should be called for this input",
                        },
                        "expected_steps": {
                            "type": "string",
                            "description": "Brief description of expected reasoning/trajectory",
                        },
                        "difficulty": {
                            "type": "string",
                            "enum": ["simple", "moderate", "complex"],
                        },
                    },
                    "required": ["question", "golden_answer", "expected_tools", "expected_steps", "difficulty"],
                },
                "description": "Test cases covering different capabilities and difficulty levels",
            },
            "evaluation_criteria": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                    },
                    "required": ["name", "description"],
                },
                "description": "Criteria for evaluating this agent (output quality, tool usage correctness, efficiency, etc.)",
            },
        },
        "required": ["agent_summary", "framework", "tools", "test_cases", "evaluation_criteria"],
    },
}


async def extract_code_from_image(image: str) -> Dict[str, str]:
    """Pull image and extract Python files from the working directory.

    Returns: dict of {filepath: content}
    """
    container_name = f"inspect_extract_{os.getpid()}"
    tmp_dir = tempfile.mkdtemp(prefix="agent_code_")

    try:
        # Create container (don't start it)
        proc = await asyncio.create_subprocess_exec(
            "docker", "create", "--name", container_name, image,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.wait()
        if proc.returncode != 0:
            stderr = await proc.stderr.read()
            raise RuntimeError(f"Failed to create container from {image}: {stderr.decode()}")

        # Get working dir from image
        proc = await asyncio.create_subprocess_exec(
            "docker", "inspect", "--format", "{{.Config.WorkingDir}}", container_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        workdir = stdout.decode().strip() or "/workspace"

        # Copy working dir contents
        proc = await asyncio.create_subprocess_exec(
            "docker", "cp", f"{container_name}:{workdir}/.", tmp_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.wait()

        # Read all Python files
        files = {}
        for py_file in Path(tmp_dir).rglob("*.py"):
            rel_path = str(py_file.relative_to(tmp_dir))
            try:
                files[rel_path] = py_file.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

        # Also check for Dockerfile to understand entrypoint
        dockerfile = Path(tmp_dir) / "Dockerfile"
        if dockerfile.exists():
            files["Dockerfile"] = dockerfile.read_text(errors="replace")

        return files

    finally:
        # Cleanup
        cleanup = await asyncio.create_subprocess_exec(
            "docker", "rm", container_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await cleanup.wait()
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def analyze_agent_deep(
    bedrock: BedrockClient,
    code_files: Dict[str, str],
    num_pairs: int = 15,
    user_context: Optional[str] = None,
) -> Dict[str, Any]:
    """Deep analysis of agent code to generate comprehensive evaluation plan."""

    # Build code context
    code_sections = []
    for filepath, content in code_files.items():
        if filepath == "Dockerfile":
            code_sections.append(f"--- Dockerfile ---\n{content}")
        else:
            code_sections.append(f"--- {filepath} ---\n{content}")

    all_code = "\n\n".join(code_sections)

    # Truncate if too long (keep first 30k chars)
    if len(all_code) > 30000:
        all_code = all_code[:30000] + "\n\n[... truncated ...]"

    context_line = f"\nUser context: {user_context}" if user_context else ""

    user_prompt = f"""Analyze this agent codebase thoroughly and generate a comprehensive evaluation plan.
{context_line}

<agent_code>
{all_code}
</agent_code>

Tasks:
1. Summarize what this agent does
2. Identify the framework (strands, crewai, langgraph, openai, anthropic, or custom)
3. List ALL tools/functions the agent can call (with their parameters)
4. Identify any sub-agents or delegation patterns
5. Generate {num_pairs} test cases that cover:
   - Simple cases (single tool call, straightforward answer)
   - Moderate cases (multi-tool, some reasoning required)
   - Complex cases (multi-step, edge cases, error handling)
   For EACH test case, specify which tools should be called and what the trajectory should look like.
6. Define evaluation criteria specific to this agent (not generic)

Focus on testing the agent's BEHAVIOR, not just its output. Include test cases that verify:
- Correct tool selection for different inputs
- Proper argument passing to tools
- Multi-step reasoning when needed
- Handling of ambiguous or edge-case inputs

Submit your complete analysis using the submit_agent_evaluation_plan tool."""

    messages = [{"role": "user", "content": user_prompt}]
    response = await asyncio.to_thread(
        bedrock.create_message,
        messages=messages,
        tools=[AGENT_DEEP_ANALYSIS_TOOL],
        tool_choice={"type": "auto"},
        system="You are an expert at analyzing AI agent code and designing comprehensive evaluations that test behavior, tool usage, and reasoning — not just final output.",
        max_tokens=16384,
    )

    tool_uses = bedrock.extract_tool_uses(response)
    if tool_uses:
        return tool_uses[0]["input"]

    return {"agent_summary": "Analysis failed", "framework": "custom", "tools": [], "test_cases": [], "evaluation_criteria": []}


async def handle_analyze_agent_image(args: Dict[str, Any]) -> List[TextContent]:
    """Handle the analyze_agent_image tool call.

    Extracts code from image, analyzes it, generates dataset + judge criteria,
    and creates the eval config — all in one step.
    """
    from backend.core.judge_config import JudgeConfig
    import importlib.util
    import os as _os
    _spec = importlib.util.spec_from_file_location(
        "create_agent_eval_config",
        _os.path.join(_os.path.dirname(__file__), "create_agent_eval_config.py")
    )
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    create_agent_eval_files = _mod.create_agent_eval_files

    try:
        image = args.get("agentImage")
        user_id = args.get("user_id")
        num_samples = args.get("numSamples", 15)
        config_name = args.get("configName", "agent_evaluation")
        model = args.get("model", "bedrock/us.anthropic.claude-sonnet-4-20250514-v1:0")
        agent_cmd = args.get("agentCmd")
        user_context = args.get("context")

        if not user_id:
            return [TextContent(type="text", text=json.dumps({"success": False, "error": "user_id is required"}))]
        if not image:
            return [TextContent(type="text", text=json.dumps({"success": False, "error": "agentImage is required"}))]

        # Step 1: Extract code from image
        try:
            code_files = await extract_code_from_image(image)
        except Exception as e:
            return [TextContent(type="text", text=json.dumps({"success": False, "error": f"Failed to extract code from image: {str(e)}"}))]

        if not code_files:
            return [TextContent(type="text", text=json.dumps({"success": False, "error": "No Python files found in image working directory"}))]

        # Step 2: Analyze the code
        bedrock = BedrockClient(region=os.environ.get("AWS_REGION", "us-west-2"))
        analysis = await analyze_agent_deep(bedrock, code_files, num_samples, user_context)

        test_cases = analysis.get("test_cases", [])
        if not test_cases:
            return [TextContent(type="text", text=json.dumps({"success": False, "error": "Could not generate test cases from agent code"}))]

        # Step 3: Determine agent command from code analysis if not provided
        if not agent_cmd:
            # Look for common entry patterns
            if "Dockerfile" in code_files:
                dockerfile = code_files["Dockerfile"]
                for line in dockerfile.split("\n"):
                    if line.strip().startswith("CMD") or line.strip().startswith("ENTRYPOINT"):
                        # Extract command, but default to python main file
                        break
            # Default: find the main .py file
            main_candidates = ["agent.py", "main.py", "app.py", "run.py"]
            for candidate in main_candidates:
                if candidate in code_files:
                    agent_cmd = ["python", candidate]
                    break
            if not agent_cmd:
                # Use first .py file
                first_py = next((f for f in code_files if f.endswith(".py") and f != "Dockerfile"), None)
                agent_cmd = ["python", first_py] if first_py else ["python", "agent.py"]

        # Step 4: Save dataset
        user_dir = get_user_dir(user_id)
        temp_dir = user_dir / "temp"
        temp_dir.mkdir(parents=True, exist_ok=True)

        dataset_name = f"agent_{config_name}"
        dataset_file = temp_dir / f"{dataset_name}.json"

        inspect_samples = []
        for tc in test_cases:
            inspect_samples.append({
                "question": tc["question"],
                "golden_answer": tc["golden_answer"],
                "expected_tools": tc.get("expected_tools", []),
                "expected_steps": tc.get("expected_steps", ""),
                "difficulty": tc.get("difficulty", "moderate"),
            })

        with open(dataset_file, "w") as f:
            json.dump(inspect_samples, f, indent=2)

        # Also save to DB for reuse
        db_tests = [{"vars": s} for s in inspect_samples]
        save_dataset_to_db(user_id, dataset_name, db_tests)

        # Step 5: Create evaluation criteria from analysis
        criteria = analysis.get("evaluation_criteria", [])
        if not criteria:
            criteria = [
                {"name": "output_correctness", "description": "Final output matches expected answer"},
                {"name": "tool_usage", "description": "Agent calls the correct tools with proper arguments"},
                {"name": "efficiency", "description": "Agent completes task without unnecessary steps"},
            ]

        judge_config = JudgeConfig(criteria=criteria)

        # Step 6: Generate eval files
        config_dir = user_dir / "configs"
        config_dir.mkdir(parents=True, exist_ok=True)

        task_code, config_data, compose_yaml = create_agent_eval_files(
            dataset_path=str(dataset_file),
            config_name=config_name,
            config_dir=str(config_dir),
            judge_config=judge_config,
            agent_image=image,
            agent_cmd=agent_cmd,
            model=model,
        )

        (config_dir / f"{config_name}.py").write_text(task_code)
        (config_dir / f"{config_name}.json").write_text(json.dumps(config_data, indent=2))
        (config_dir / "compose.yaml").write_text(compose_yaml)

        result = {
            "success": True,
            "configName": config_name,
            "summary": {
                "agent_summary": analysis.get("agent_summary", ""),
                "framework": analysis.get("framework", "unknown"),
                "tools_found": [t["name"] for t in analysis.get("tools", [])],
                "subagents": [s["name"] for s in analysis.get("subagents", [])],
                "test_cases": len(test_cases),
                "criteria": [c["name"] for c in criteria],
                "difficulty_breakdown": {
                    "simple": sum(1 for tc in test_cases if tc.get("difficulty") == "simple"),
                    "moderate": sum(1 for tc in test_cases if tc.get("difficulty") == "moderate"),
                    "complex": sum(1 for tc in test_cases if tc.get("difficulty") == "complex"),
                },
                "agentImage": image,
                "agentCmd": agent_cmd,
                "model": model,
            },
            "nextStep": f"Run evaluation: run_evaluation(configName='{config_name}')",
        }

        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    except Exception as e:
        logger.exception("Failed to analyze agent image")
        return [TextContent(type="text", text=json.dumps({"success": False, "error": f"Failed to analyze agent: {str(e)}"}))]
