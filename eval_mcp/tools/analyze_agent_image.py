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

from eval_mcp.core.bedrock_client import BedrockClient
from eval_mcp.core.user_storage import get_user_dir, save_dataset_to_db

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
                            "description": "Tools that should be called for this input. Leave as empty array if the agent has no tools.",
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
                    "required": ["question", "golden_answer", "expected_steps", "difficulty"],
                },
                "description": "Test cases covering different capabilities and difficulty levels",
            },
            "pipeline_stages": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "snake_case stage identifier"},
                        "display_name": {"type": "string", "description": "Human-readable stage name"},
                        "order": {"type": "integer", "description": "Execution order (1 = first)"},
                        "scorer_type": {"type": "string", "enum": ["deterministic", "llm_judge"], "description": "deterministic for simple checks (tool called, text included), llm_judge for quality assessment"},
                        "check": {"type": "string", "description": "For deterministic only: 'tool_called' or 'includes_text'"},
                        "expected_field": {"type": "string", "description": "For deterministic only: dataset metadata field with expected value (e.g. 'expected_tools')"},
                        "criteria": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string", "description": "snake_case criterion name"},
                                    "description": {"type": "string"},
                                },
                                "required": ["name", "description"],
                            },
                            "description": "For llm_judge only: criteria to score on",
                        },
                        "context_filter": {"type": "string", "enum": ["all", "first_response", "tool_calls_only", "final_output"], "description": "What part of the agent trace to show the judge"},
                    },
                    "required": ["name", "display_name", "order", "scorer_type"],
                },
                "description": "Evaluation pipeline stages. Design stages that match THIS agent's architecture. Examples: routing stage (deterministic, check orchestrator picks right sub-agent), tool_execution (deterministic, check correct tools called), argument_quality (llm_judge on tool args), final_output (llm_judge on answer). Adapt to what you see in the code.",
            },
        },
        "required": ["agent_summary", "framework", "tools", "test_cases", "pipeline_stages"],
    },
}


async def extract_code_from_image(image: str) -> Dict[str, str]:
    """Extract Python files from a container image.

    Uses kubectl (EKS) or docker (local) to start a temporary container,
    copy out the code, and delete it. Works with any registry the
    environment can pull from — no extra auth needed.

    Returns: dict of {filepath: content}
    """
    tmp_dir = tempfile.mkdtemp(prefix="agent_code_")
    pod_name = f"extract-{os.getpid()}-{int(asyncio.get_event_loop().time())}"

    try:
        extracted = False

        # Try kubectl first (works in EKS)
        try:
            namespace = os.environ.get("K8S_AGENT_NAMESPACE", os.environ.get("NAMESPACE", "eval-managed"))

            # Create temp pod
            proc = await asyncio.create_subprocess_exec(
                "kubectl", "run", pod_name, "-n", namespace,
                f"--image={image}", "--restart=Never",
                "--", "sleep", "120",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            await proc.wait()
            if proc.returncode != 0:
                raise RuntimeError("kubectl run failed")

            # Wait for pod to be ready
            proc = await asyncio.create_subprocess_exec(
                "kubectl", "wait", f"pod/{pod_name}", "-n", namespace,
                "--for=condition=Ready", "--timeout=60s",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            await proc.wait()
            if proc.returncode != 0:
                raise RuntimeError("pod not ready")

            # Get working dir
            proc = await asyncio.create_subprocess_exec(
                "kubectl", "exec", pod_name, "-n", namespace, "--", "pwd",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            workdir = stdout.decode().strip() or "/workspace"

            # List and copy .py files from working dir
            proc = await asyncio.create_subprocess_exec(
                "kubectl", "exec", pod_name, "-n", namespace, "--",
                "find", workdir, "-maxdepth", "3", "-name", "*.py", "-type", "f",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            py_files = [f.strip() for f in stdout.decode().split("\n") if f.strip()]

            for remote_path in py_files:
                proc = await asyncio.create_subprocess_exec(
                    "kubectl", "exec", pod_name, "-n", namespace, "--", "cat", remote_path,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await proc.communicate()
                if proc.returncode == 0:
                    rel_path = remote_path.replace(workdir + "/", "").lstrip("/")
                    local_path = Path(tmp_dir) / rel_path
                    local_path.parent.mkdir(parents=True, exist_ok=True)
                    local_path.write_text(stdout.decode("utf-8", errors="replace"))

            extracted = True

        except Exception:
            pass
        finally:
            try:
                cleanup = await asyncio.create_subprocess_exec(
                    "kubectl", "delete", "pod", pod_name, "-n",
                    os.environ.get("K8S_AGENT_NAMESPACE", os.environ.get("NAMESPACE", "eval-managed")),
                    "--ignore-not-found", "--grace-period=0",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                await cleanup.wait()
            except Exception:
                pass

        # Fall back to docker (works for local images)
        if not extracted:
            container_name = f"inspect_extract_{os.getpid()}"
            try:
                proc = await asyncio.create_subprocess_exec(
                    "docker", "create", "--name", container_name, image,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                await proc.wait()
                if proc.returncode != 0:
                    raise RuntimeError("docker create failed")

                proc = await asyncio.create_subprocess_exec(
                    "docker", "inspect", "--format", "{{.Config.WorkingDir}}", container_name,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await proc.communicate()
                workdir = stdout.decode().strip() or "/"

                proc = await asyncio.create_subprocess_exec(
                    "docker", "cp", f"{container_name}:{workdir}/.", tmp_dir,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                await proc.wait()
                extracted = True
            finally:
                cleanup = await asyncio.create_subprocess_exec(
                    "docker", "rm", container_name,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                await cleanup.wait()

        if not extracted:
            raise RuntimeError(f"Could not extract code from {image}")

        # Read all extracted Python files
        files = {}
        for py_file in Path(tmp_dir).rglob("*.py"):
            rel_path = str(py_file.relative_to(tmp_dir))
            if "site-packages" in rel_path or "/usr/lib/" in rel_path:
                continue
            try:
                content = py_file.read_text(encoding="utf-8", errors="replace")
                if len(content) < 50000:
                    files[rel_path] = content
            except Exception:
                continue

        return files

    finally:
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
3. List ALL Bedrock tools the agent exposes to the model — i.e., things the LLM can decide to invoke during inference. Concrete sources to look for:
   - Items in `toolConfig.tools` passed to bedrock-runtime.converse / converse_stream
   - `@tool` decorators (Strands, LangChain) registered on an agent
   - Tool registries the agent constructs and passes to a framework

   DO NOT include internal Python helper functions that the agent code calls itself. Only count things the LLM can choose to invoke.
   If the agent uses no tools at all, return an empty array.
4. Identify any sub-agents or delegation patterns
5. Generate {num_pairs} test cases that cover:
   - Simple cases (single tool call, straightforward answer)
   - Moderate cases (multi-tool, some reasoning required)
   - Complex cases (multi-step, edge cases, error handling)
   For EACH test case, specify which tools should be called and what the trajectory should look like.
6. Design a PIPELINE of evaluation stages tailored to THIS agent's architecture.
   Each stage evaluates one aspect of behavior with its own scorer.

   For deterministic stages (simple checks, no LLM needed):
   - "tool_called": checks if specific tools were invoked (set expected_field to the test case field that has expected tools)
   - "includes_text": checks if output contains expected text

   For llm_judge stages (quality assessment):
   - Define specific criteria (snake_case names only)
   - Set context_filter to control what the judge sees

   Examples of good pipeline designs:
   - Simple agent: [tool_selection (deterministic), final_output (llm_judge)]
   - Multi-agent: [routing (deterministic, did orchestrator pick right sub-agent), sub_agent_tools (deterministic, did sub-agent use right tools), argument_quality (llm_judge), final_output (llm_judge)]
   - RAG agent: [retrieval (deterministic, right docs fetched), context_usage (llm_judge), answer_quality (llm_judge)]
   - Converse-only agent (NO tools): [output_correctness (llm_judge), reasoning_quality (llm_judge), output_format (llm_judge)] — when the agent makes plain text completions without any tool-calling, design stages that score the output and reasoning. Do NOT include tool_called stages.

   Adapt the pipeline to what you see in the code. More complex agents need more stages.

   IMPORTANT: A `tool_called` stage only makes sense when the agent actually exposes Bedrock tools (the things you identified in step 3). If step 3 returned an empty array, do NOT generate any `tool_called` stages or populate `expected_tools` on test cases. Use llm_judge stages on output, reasoning, and trajectory instead.

Focus on testing the agent's BEHAVIOR, not just its output.

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

    return {"agent_summary": "Analysis failed", "framework": "custom", "tools": [], "test_cases": [], "pipeline_stages": []}


async def handle_analyze_agent_image(args: Dict[str, Any]) -> List[TextContent]:
    """Handle the analyze_agent_image tool call.

    Extracts code from image, analyzes it, generates dataset + pipeline stages,
    and creates the eval config — all in one step.
    """
    from eval_mcp.core.pipeline_stages import PipelineConfig, PipelineStage
    from eval_mcp.core.judge_config import JUDGE_MODELS
    import importlib.util
    import os as _os
    _spec = importlib.util.spec_from_file_location(
        "create_pipeline_eval_config",
        _os.path.join(_os.path.dirname(__file__), "create_pipeline_eval_config.py")
    )
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    create_pipeline_eval_files = _mod.create_pipeline_eval_files

    try:
        import time
        image = args.get("agentImage")
        user_id = args.get("user_id")
        num_samples = args.get("numSamples", 15)
        # Auto-generated name — agents never pick.
        config_name = f"agent_eval_{int(time.time() * 1000)}"
        model = args.get("model")
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

        # Also save to DB for reuse
        db_tests = [{"vars": s} for s in inspect_samples]
        save_dataset_to_db(
            user_id,
            dataset_name,
            db_tests,
            source={"kind": "synthetic", "mode": "agent-image"},
        )

        # Step 5: Build pipeline stages from analysis
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

        # Step 6: Generate eval files
        config_dir = user_dir / "configs"
        config_dir.mkdir(parents=True, exist_ok=True)

        task_code, config_data, compose_yaml, k8s_values = create_pipeline_eval_files(
            dataset_path=str(dataset_file),
            config_name=config_name,
            config_dir=str(config_dir),
            pipeline=pipeline,
            judge_models=JUDGE_MODELS,
            agent_image=image,
            agent_cmd=agent_cmd,
            model=model,
        )

        (config_dir / f"{config_name}.py").write_text(task_code)
        (config_dir / f"{config_name}.json").write_text(json.dumps(config_data, indent=2))
        (config_dir / "compose.yaml").write_text(compose_yaml)
        (config_dir / "values.yaml").write_text(k8s_values)

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
