#!/usr/bin/env python3
"""
Unified Eval MCP Server.

Single MCP server combining all evaluation tools: dataset management,
model discovery, evaluation creation/execution, and results exploration.

Supports both stdio (local Claude Code) and HTTP (deployed) transports.
"""

import json
import os
import sys
from pathlib import Path

from mcp.server import FastMCP

from eval_mcp.core.bedrock_client import BedrockClient
from eval_mcp.core.user_storage import list_user_document_paths
from eval_mcp.tools.agent import DatasetAgent
from eval_mcp.tools.save_dataset import handle_save_dataset
from eval_mcp.tools.generate_qa import handle_generate_qa_pairs
from eval_mcp.tools.generate_judge import handle_generate_judge
from eval_mcp.tools.create_config import handle_create_eval_config
from eval_mcp.tools.create_agent_eval_config import handle_create_agent_eval_config
from eval_mcp.tools.analyze_agent_image import handle_analyze_agent_image
from eval_mcp.tools.analyze_agent_path import handle_analyze_agent_path
from eval_mcp.tools.list_datasets import handle_list_datasets
from eval_mcp.tools.list_judges import handle_list_judges
from eval_mcp.tools.list_evaluations import handle_list_evaluations
from eval_mcp.tools.get_evaluation_details import handle_get_evaluation_details
from eval_mcp.tools.run_eval import (
    handle_run_evaluation,
    handle_retry_evaluation,
    cancel_user_evaluation,
    get_running_eval_info,
)
from eval_mcp.tools.generate_report import handle_generate_report

# Configuration
region = os.environ.get("AWS_REGION", "us-west-2")
port = int(os.environ.get("EVAL_MCP_PORT", "8002"))
host = os.environ.get("HOST", "127.0.0.1")

# Default user for local/standalone mode (no multi-tenant)
DEFAULT_USER = os.environ.get("EVAL_MCP_USER", "local")

# Set default storage to ~/.eval-mcp if not explicitly configured
if "USER_STORAGE_BASE" not in os.environ:
    os.environ["USER_STORAGE_BASE"] = str(Path.home() / ".eval-mcp" / "users")

# Initialize server
mcp = FastMCP("eval-server", port=port, host=host)

# Shared clients
bedrock = BedrockClient(region=region)


def _user(user_id: str = None) -> str:
    return user_id or DEFAULT_USER


def _auto_pull(user_id: str = None) -> None:
    """Pull missing files from S3 before serving a read. No-op if no bucket
    configured; debounced so back-to-back tool calls share one pull.
    """
    try:
        from eval_mcp.s3_sync import auto_pull
        auto_pull(user_id=_user(user_id))
    except Exception:
        pass


# ============================================================
# Dataset tools
# ============================================================

@mcp.tool()
async def analyze_dataset(
    file_path: str = None,
    file_content: str = None,
    filename: str = None,
    user_id: str = None,
) -> str:
    """
    Analyze a CSV/JSON/JSONL dataset for structure and quality.

    Uses an intelligent agent to parse the file, detect structure,
    identify question/answer columns, and check for data quality issues.

    Prefer `file_path` — the tool reads the file from disk. Pass `file_content`
    only if the data isn't on the local filesystem.

    Args:
        file_path: Absolute path to the dataset file (recommended).
        file_content: Raw file content as a string (fallback).
        filename: Optional display name (inferred from file_path when omitted).

    Returns:
        JSON analysis report with validity, column mapping, issues, and summary.
    """
    if file_path and not file_content:
        try:
            file_content = Path(file_path).read_text()
            if not filename:
                filename = Path(file_path).name
        except Exception as e:
            return json.dumps({"success": False, "error": f"Could not read file_path {file_path!r}: {e}"})
    if not filename:
        filename = "dataset.csv"
    if not file_content:
        return json.dumps({"success": False, "error": "Provide either file_path or file_content"})

    agent = DatasetAgent(bedrock)
    analysis = await agent.analyze(file_content, filename)
    return json.dumps({"success": True, "filename": filename, "analysis": analysis}, indent=2)


@mcp.tool()
async def save_dataset(
    column_mapping: dict,
    file_path: str = None,
    file_content: str = None,
    filename: str = None,
    user_id: str = None,
) -> str:
    """
    Save a CSV/JSON/JSONL dataset for evaluation.

    Converts the file to the canonical {question, golden_answer} format and
    persists it. Prefer `file_path` — the tool reads from disk (cheap).
    Pass `file_content` only if the data isn't on the local filesystem.

    Args:
        column_mapping: {"question": col_name, "golden_answer": col_name}
        file_path: Absolute path to the dataset file (recommended).
        file_content: Raw file content as a string (fallback).
        filename: Optional display name (inferred from file_path when omitted).

    Returns:
        JSON with success status, generated dataset name, and rows saved.
    """
    args = {
        "file_path": file_path,
        "file_content": file_content,
        "filename": filename,
        "column_mapping": column_mapping,
        "user_id": _user(user_id),
    }
    result = await handle_save_dataset(args)
    return result[0].text


# ============================================================
# Provider/model discovery tools
# ============================================================

from eval_mcp.tools.bedrock_models import (
    list_bedrock_models as _list_bedrock_models,
    list_available_models as _list_available_models,
)


@mcp.tool()
def list_bedrock_models(
    provider: str = "all",
    limit: int = 0,
    text_only: bool = True,
) -> str:
    """
    Get list of AWS Bedrock models available for evaluations.

    Queries inference profiles (paginated) and foundation models, dedupes, and
    returns entries in bedrock/* form ready to pass to eval configs.

    Args:
        provider: Filter by provider name (case-insensitive): "all", "anthropic",
            "meta", "mistral", "amazon", "deepseek", "nvidia", etc.
        limit: Max models to return (0 = unlimited).
        text_only: If True (default), exclude image/embedding models.
    """
    return json.dumps(
        _list_bedrock_models(provider=provider, limit=limit, text_only=text_only),
        indent=2,
    )


@mcp.tool()
def list_available_models(
    provider: str = "all",
    source: str = "all",
) -> str:
    """
    List all models available for evaluations, across Bedrock and external providers.

    External providers appear only when their API key env var is set
    (OPENAI_API_KEY, ANTHROPIC_API_KEY, GOOGLE_API_KEY).

    Args:
        provider: Filter by provider name (case-insensitive).
        source: "all" | "bedrock" | "external".
    """
    return json.dumps(
        _list_available_models(provider=provider, source=source),
        indent=2,
    )


# ============================================================
# Evaluation tools
# ============================================================

@mcp.tool()
async def generate_qa_pairs(
    user_id: str = None,
    prompt: str = None,
    documents: list = None,
    instructions: str = None,
    numSamples: int = 10,
    numPersonas: int = 5,
) -> str:
    """
    Generate question-answer pairs with golden answers for LLM-as-judge evaluation.

    Supports three modes:
    1. Agent mode: Provide .py file to analyze agent code, generate QA pairs, and create eval wrapper
    2. Document mode: Provide 'documents' list to generate QA from uploaded files (PDFs, images, text)
    3. Persona mode: Provide 'prompt' to generate synthetic QA from diverse personas

    Args:
        prompt: AI system purpose/description (required for persona mode, optional context for others)
        documents: List of document paths from user's documents folder
        instructions: Additional instructions for QA generation
        numSamples: Number of QA pairs to generate (default: 10)
        numPersonas: Number of personas for synthetic generation (default: 5, persona mode only)

    Returns:
        JSON with dataset name and summary. Use with generate_judge and create_eval_config.
    """
    args = {
        "prompt": prompt or "",
        "user_id": _user(user_id),
        "documents": documents or [],
        "instructions": instructions,
        "numSamples": numSamples,
        "numPersonas": numPersonas,
    }
    result = await handle_generate_qa_pairs(bedrock, args)
    return result[0].text


@mcp.tool()
async def list_documents(user_id: str = None) -> str:
    """
    List all uploaded documents available for the user.

    Use this to discover existing documents that can be used with generate_qa_pairs.
    Returns document paths that can be passed to generate_qa_pairs(documents=[...]).

    Returns:
        JSON with list of document paths
    """
    try:
        paths = list_user_document_paths(_user(user_id))
        return json.dumps({
            "success": True,
            "documents": paths,
            "count": len(paths),
            "hint": "Pass these paths to generate_qa_pairs(documents=[...]) to generate QA pairs from documents",
        })
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
async def generate_judge(
    dataset: str,
    user_id: str = None,
    domain: str = "general",
) -> str:
    """
    Generate an LLM judge configuration tailored to your dataset.

    Analyzes QA pairs to determine appropriate evaluation criteria and
    creates a judge configuration ready for use in evaluations.

    Args:
        dataset: Name of dataset from list_datasets
        domain: Domain hint (e.g., "medical", "legal", "technical"). Default: "general"

    Returns:
        JSON with judge name and criteria summary
    """
    args = {
        "dataset": dataset,
        "user_id": _user(user_id),
        "domain": domain,
    }
    result = await handle_generate_judge(bedrock, args)
    return result[0].text


@mcp.tool()
async def create_eval_config(
    dataset: str,
    providers: list,
    judge: str,
    user_id: str = None,
    prompts: str | list = "{question}",
    description: str = None,
    judge_models: list = None,
    agent_path: str = None,
    agent_entry: str = None,
) -> str:
    """
    Create an Inspect AI evaluation configuration with multi-judge support.

    Generates config with LLM judges that evaluate using binary scores.
    Results are aggregated by Jury scoring. The config name is auto-generated
    from a timestamp — you do NOT pick it. This prevents accidentally reusing
    a stale config by name.

    For agent evaluations: pass agent_path to evaluate a local Python agent
    with full Bedrock call tracing. The agent code is not modified.

    Args:
        dataset: Name of dataset from list_datasets
        providers: List of target models to evaluate (used for jury judges routing).
            For agent evals, the agent calls Bedrock directly.
        judge: Name of judge from list_judges (REQUIRED - criteria adapted to QA pairs)
        prompts: Single prompt string OR list of prompts for comparison. Use {question} or {prompt} as placeholder.
        description: Optional description of the evaluation
        judge_models: Optional list of model IDs to use as judges
        agent_path: Path to a Python agent file to evaluate. The agent must have a callable entry function.
        agent_entry: Name of the entry function in the agent file (default: "run_agent")

    Returns:
        JSON with the auto-generated configName and summary. Pass that configName
        straight to run_evaluation / run_evaluation_and_report.
    """
    args = {
        "dataset": dataset,
        "providers": providers,
        "judge": judge,
        "user_id": _user(user_id),
        "prompts": prompts,
        "description": description,
        "judge_models": judge_models,
        "agent_path": agent_path,
        "agent_entry": agent_entry,
    }
    result = await handle_create_eval_config(args)
    return result[0].text


@mcp.tool()
async def create_agent_eval_config(
    dataset: str,
    judge: str,
    agentImage: str,
    user_id: str = None,
    agentCmd: list = None,
    model: str = None,
    description: str = None,
    judge_models: list = None,
) -> str:
    """
    Create an evaluation config for testing an agent running in a container.

    The config name is auto-generated (timestamp). Returned configName should
    be passed straight to run_evaluation.

    The agent runs in a Docker container with LLM calls intercepted via proxy.
    All model calls, tool usage, and token consumption are captured automatically.

    Agent contract:
    - Agent must use OpenAI or Anthropic SDK format
    - Agent reads OPENAI_BASE_URL or ANTHROPIC_BASE_URL from environment
    - Agent accepts a prompt as the last CLI argument
    - Agent prints its response to stdout

    Args:
        dataset: Name of dataset from list_datasets
        judge: Name of judge from list_judges
        agentImage: Container image URI (e.g., "123456.dkr.ecr.us-east-2.amazonaws.com/my-agent:latest")
        agentCmd: Command to run the agent (default: ["python", "agent.py"])
        model: Model to route agent LLM requests through (only needed if agent uses model="inspect")
        description: Optional description of the evaluation
        judge_models: Optional list of model IDs to use as judges

    Returns:
        JSON with the auto-generated configName and summary.
    """
    args = {
        "dataset": dataset,
        "judge": judge,
        "agentImage": agentImage,
        "user_id": _user(user_id),
        "agentCmd": agentCmd or ["python", "agent.py"],
        "model": model,
        "description": description,
        "judge_models": judge_models,
    }
    result = await handle_create_agent_eval_config(args)
    return result[0].text


@mcp.tool()
async def analyze_agent_image(
    agentImage: str,
    user_id: str = None,
    numSamples: int = 15,
    agentCmd: list = None,
    model: str = None,
    context: str = None,
) -> str:
    """
    Analyze an agent container image and generate a complete evaluation automatically.

    Extracts code from the image, analyzes tools/subagents/logic, generates
    test cases covering output correctness, tool usage, and trajectory,
    and creates the eval config ready to run.

    This is the ONE-STEP agent evaluation tool. User provides an image, gets
    a complete evaluation config with no other setup needed.

    Args:
        agentImage: Container image URI (ECR, DockerHub, GHCR, etc.)
        numSamples: Number of test cases to generate (default: 15)
        agentCmd: Command to run the agent (auto-detected if not provided)
        model: Model to route agent's LLM requests to
        context: Optional user description of what the agent should do

    Returns:
        JSON with eval config ready to run, including the auto-generated configName.
    """
    args = {
        "agentImage": agentImage,
        "user_id": _user(user_id),
        "numSamples": numSamples,
        "agentCmd": agentCmd,
        "model": model,
        "context": context,
    }
    result = await handle_analyze_agent_image(args)
    return result[0].text


@mcp.tool()
async def analyze_agent_path(
    agentPath: str,
    user_id: str = None,
    agentEntry: str = "run_agent",
    numSamples: int = 15,
    context: str = None,
) -> str:
    """
    Analyze a local Python agent and generate a complete agentic evaluation.

    Reads the agent code from disk, has Claude analyze tools/sub-agents/logic,
    generates rich test cases (with expected tools and trajectory per case),
    designs a pipeline of evaluation stages tailored to THIS agent's
    architecture (e.g. routing → tool selection → argument quality → final
    output), and writes a runnable eval config.

    Bedrock calls made by the agent during evaluation are captured via
    OpenTelemetry — no Docker, no agent code modification required.

    Use this for fully agentic evals (multi-stage scoring with trajectory).
    For non-agentic prompt comparison, use create_eval_config.

    Args:
        agentPath: Path to the user's Python agent file (must define run_agent
            or another callable taking a prompt string and returning a string).
        agentEntry: Name of the entry function (default: "run_agent")
        numSamples: Number of test cases to generate (default: 15)
        context: Optional user description of what the agent should do

    Returns:
        JSON with eval config ready to run, including the auto-generated
        configName, analysis summary, and the pipeline stages designed for
        this agent.
    """
    args = {
        "agentPath": agentPath,
        "agentEntry": agentEntry,
        "user_id": _user(user_id),
        "numSamples": numSamples,
        "context": context,
    }
    result = await handle_analyze_agent_path(args)
    return result[0].text


@mcp.tool()
async def list_datasets(
    user_id: str = None,
    searchTerm: str = None,
) -> str:
    """
    List available datasets.

    Returns details about each dataset including number of samples and preview.
    Dataset names can be used with generate_judge and create_eval_config.

    Args:
        searchTerm: Optional search term to filter datasets by name

    Returns:
        Formatted list of datasets with details
    """
    _auto_pull(user_id)
    args = {"user_id": _user(user_id), "searchTerm": searchTerm}
    result = await handle_list_datasets(args)
    return result[0].text


@mcp.tool()
async def list_judges(
    user_id: str = None,
    searchTerm: str = None,
) -> str:
    """
    List available LLM judges.

    Returns details about each judge including domain and evaluation criteria.
    Judge names can be used with create_eval_config.

    Args:
        searchTerm: Optional search term to filter judges by name

    Returns:
        Formatted list of judges with details
    """
    _auto_pull(user_id)
    args = {"user_id": _user(user_id), "searchTerm": searchTerm}
    result = await handle_list_judges(args)
    return result[0].text


@mcp.tool()
async def list_evaluations(
    user_id: str = None,
    limit: int = 20,
) -> str:
    """
    List completed evaluations.

    Each entry returns a `score` object with:
      - metrics.overall: the same 0.0-1.0 rubric average shown in the UI
        (mean of per-criterion scores, no pass/fail threshold)
      - byCriterion: per-criterion 0.0-1.0 breakdown (Core Claim, Terminology,
        Factual, Coverage, Reasoning — whatever the judge emitted)

    Args:
        limit: Maximum number of evaluations to return (default: 20)

    Returns:
        JSON with list of evaluations and their aggregated scores.
    """
    _auto_pull(user_id)
    args = {"user_id": _user(user_id), "limit": limit}
    result = await handle_list_evaluations(args)
    return result[0].text


@mcp.tool()
async def get_evaluation_details(
    evalId: str,
    user_id: str = None,
) -> str:
    """
    Get detailed results for a specific evaluation.

    Returns full results including individual test outcomes, scores, and grading details.

    Args:
        evalId: The evaluation ID to retrieve (from list_evaluations)

    Returns:
        JSON with detailed evaluation results
    """
    _auto_pull(user_id)
    args = {"evalId": evalId, "user_id": _user(user_id)}
    result = await handle_get_evaluation_details(args)
    return result[0].text


@mcp.tool()
async def run_evaluation(
    configName: str,
    user_id: str = None,
) -> str:
    """
    Low-level runner for an already-built config. Most callers should use
    `run_evaluation_and_report` instead — that's the one-shot path that
    auto-generates dataset/judge/config and writes a PDF report.

    Only reach for this tool when you already have a configName from a
    prior `create_eval_config` call and specifically do NOT want the report step.

    Flow:
    1. Runs target model(s) via Inspect AI
    2. Each response evaluated by judges
    3. Results written to .eval log files for viewing

    Concurrency is auto-tuned by Inspect based on provider throttling.

    Args:
        configName: Name of an existing eval config (from create_eval_config).

    Returns:
        JSON with evaluation results including scores.
    """
    args = {
        "configName": configName,
        "user_id": _user(user_id),
    }
    result = await handle_run_evaluation(args)
    return result[0].text


@mcp.tool()
async def run_evaluation_and_report(
    user_id: str = None,
    # Standard / prompt-comparison eval inputs (all optional — missing pieces are generated)
    providers: list = None,
    dataset: str = None,
    judge: str = None,
    prompts: str | list = "{question}",
    description: str = None,
    judge_models: list = None,
    documents: list = None,
    # Agent eval inputs (alternative path)
    agent_path: str = None,
    agent_entry: str = "run_agent",
    num_samples: int = 15,
    # Expert mode: user-authored Inspect AI task.py
    task_path: str = None,
    # Shared
    context: str = None,
    monthly_volume: int = 10000,
) -> str:
    """
    DEFAULT entry point for running an eval. Use this for any "run an eval" request.

    Pass `providers=["bedrock/..."]` and, optionally, `dataset` / `judge`.
    Anything you omit is auto-generated: QA pairs, judge, config. Writes a
    PDF report at the end. One call — no need to chain list_datasets +
    list_judges + create_eval_config + run_evaluation.

    Minimal call for a sample eval:
        run_evaluation_and_report(providers=["bedrock/us.anthropic.claude-sonnet-4-6"])

    Modes (reference — you only pass what distinguishes the mode):

    A. Agent eval:
         agent_path (+ agent_entry, num_samples, context)
         Everything else (dataset, judge, pipeline) is generated from the code.

    B. Standard eval:
         providers
         If `dataset` omitted: generate_qa_pairs runs first (uses `documents`
         if provided, otherwise synthesizes from `context`).
         If `judge` omitted: generate_judge runs on the resulting dataset.

    C. Prompt comparison:
         providers + prompts=[list of templates]
         Same auto-generation of dataset/judge as B.

    D. Expert mode — user-authored Inspect AI task:
         task_path (+ optional providers, context)
         Runs your own `task.py` with whatever Inspect AI scorers/solvers you
         chose (exact_match, model_graded_qa, pass_at_k, etc.). The tool runs
         `inspect eval <task_path>`, captures the log, generates a report, and
         opens the viewer. Use this when the jury-scoring pipeline isn't what
         you want. Check `inspect --help` and inspect_ai source for available
         scorers and task helpers.

    Args:
        providers: Target model IDs, e.g. ["bedrock/us.anthropic.claude-sonnet-4-6"].
            Required for modes B/C.
        dataset: Name of an existing dataset (from list_datasets). Optional —
            auto-generated when omitted.
        judge: Name of an existing judge (from list_judges). Optional —
            auto-generated from the dataset + context when omitted.
        prompts: Single prompt template, or list of prompts for a comparison.
            Use {question} or {prompt} as the placeholder. Default: "{question}"
        description: Optional description recorded in the eval.
        judge_models: Optional override list of judge model IDs.
        documents: Optional list of document paths to ground dataset generation
            (PDFs, markdown, etc.) when auto-generating a dataset.

        agent_path: Path to the user's Python agent file (mode A).
        agent_entry: Entry function name (default: "run_agent").
        num_samples: Number of test cases (default: 15).

        context: Short description of what the user is evaluating and why.
            Used to tailor auto-generated datasets/judges AND the report narrative.
        monthly_volume: Projected monthly call volume for cost projections.

    Returns:
        JSON combining eval results, the auto-generated configName, any
        auto-generated dataset/judge names, and the report download URL.
    """
    uid = _user(user_id)
    generated: dict = {}  # track what was auto-created so the caller can see

    # Mode D: expert — run a user-authored Inspect AI task.py as-is.
    # We copy it into the user's configs/ dir so the standard run pipeline
    # (log dir, retry, S3 sync, viewer) picks it up without special-casing.
    if task_path:
        import shutil
        import time as _time
        from eval_mcp.core.user_storage import get_user_dir

        src = Path(task_path)
        if not src.exists() or not src.is_file():
            return json.dumps({
                "success": False,
                "error": f"task_path does not exist: {task_path}",
            }, indent=2)

        config_name = f"custom_task_{int(_time.time() * 1000)}"
        user_dir = get_user_dir(uid)
        configs_dir = user_dir / "configs"
        configs_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, configs_dir / f"{config_name}.py")
        generated["task_path"] = str(src)

    # Mode A: agent eval — analyze_agent_path already auto-generates everything
    # from the agent code (dataset, judge, pipeline stages, config).
    elif agent_path:
        analyze_result = await handle_analyze_agent_path({
            "agentPath": agent_path,
            "agentEntry": agent_entry,
            "user_id": uid,
            "numSamples": num_samples,
            "context": context,
        })
        analyze_data = json.loads(analyze_result[0].text)
        if not analyze_data.get("success"):
            return analyze_result[0].text
        config_name = analyze_data["configName"]
    elif not task_path:
        # Modes B / C: standard or prompt-comparison eval.
        if not providers:
            return json.dumps({
                "success": False,
                "error": "providers is required (list of model IDs). "
                         "For an agent eval pass agent_path instead.",
            }, indent=2)

        # Auto-generate dataset if one wasn't named.
        if not dataset:
            qa_args = {
                "user_id": uid,
                "numSamples": num_samples,
                "prompt": context or description or "",
            }
            if documents:
                # `documents` may be either registered doc names (already under
                # the user's documents dir) OR absolute local paths to arbitrary
                # files. For the latter we copy into the user's documents dir so
                # generate_qa_pairs → get_document_content can find them.
                import shutil as _shutil
                from eval_mcp.core.user_storage import get_user_documents_dir
                docs_dir = get_user_documents_dir(uid)
                resolved_docs = []
                for doc in documents:
                    p = Path(doc)
                    if p.is_absolute() and p.is_file():
                        dest = docs_dir / p.name
                        if not dest.exists() or dest.stat().st_size != p.stat().st_size:
                            _shutil.copy2(p, dest)
                        resolved_docs.append(p.name)
                    else:
                        resolved_docs.append(doc)
                qa_args["documents"] = resolved_docs
            qa_result = await handle_generate_qa_pairs(bedrock, qa_args)
            qa_data = json.loads(qa_result[0].text)
            if not qa_data.get("success"):
                return qa_result[0].text
            dataset = qa_data["dataset"]
            generated["dataset"] = dataset

        # Auto-generate judge if one wasn't named.
        if not judge:
            j_args = {
                "user_id": uid,
                "dataset": dataset,
                "domain": context or description or "general",
            }
            j_result = await handle_generate_judge(bedrock, j_args)
            j_data = json.loads(j_result[0].text)
            if not j_data.get("success"):
                return j_result[0].text
            judge = j_data["name"]
            generated["judge"] = judge

        create_result = await handle_create_eval_config({
            "dataset": dataset,
            "providers": providers,
            "judge": judge,
            "user_id": uid,
            "prompts": prompts,
            "description": description,
            "judge_models": judge_models,
        })
        create_data = json.loads(create_result[0].text)
        if not create_data.get("success"):
            return create_result[0].text

        config_name = create_data["configName"]

    # Suppress the eval's auto-open: the viewer would load before the PDF
    # is written and show "no report has been generated yet". We open it
    # ourselves once the report is on disk.
    eval_result = await handle_run_evaluation({
        "configName": config_name,
        "user_id": uid,
        "openViewer": False,
    })
    eval_data = json.loads(eval_result[0].text)
    eval_data["configName"] = config_name
    if generated:
        eval_data["autoGenerated"] = generated

    if not eval_data.get("success"):
        return json.dumps(eval_data, indent=2)  # propagate eval failure as-is

    run_id = eval_data.get("runId")
    if not run_id:
        eval_data["reportStatus"] = "skipped: no runId returned from eval"
        return json.dumps(eval_data, indent=2)

    report_result = await handle_generate_report({
        "user_id": uid,
        "group_id": run_id,
        "context": context,
        "monthly_volume": monthly_volume,
    })
    report_data = json.loads(report_result[0].text)

    eval_data["report"] = report_data

    viewer_path = f"/results?group={run_id}"
    try:
        from eval_mcp.viewer import ensure_viewer_running
        info = ensure_viewer_running(port=4001, open_path=viewer_path)
        eval_data["viewerUrl"] = info["url"]
        if info.get("browserOpened"):
            eval_data["viewResults"] = (
                f"Viewer already running; opened {info['url']}"
                if info.get("alreadyRunning")
                else f"Started viewer and opened {info['url']}"
            )
        elif info.get("error"):
            eval_data["viewResults"] = (
                f"Could not auto-start viewer ({info['error']}). "
                f"Run `eval-mcp view` manually, then open {info['url']}"
            )
    except Exception as e:
        eval_data["viewResults"] = (
            f"Run `eval-mcp view` in your terminal, then open "
            f"http://localhost:4001{viewer_path} ({e})"
        )

    return json.dumps(eval_data, indent=2)


@mcp.tool()
async def retry_evaluation(
    user_id: str = None,
) -> str:
    """
    Retry failed or incomplete evaluations.

    Finds evaluations that failed or were interrupted and retries only
    the incomplete samples. Concurrency is auto-tuned by Inspect.

    Returns:
        JSON with retry results
    """
    args = {"user_id": _user(user_id)}
    result = await handle_retry_evaluation(args)
    return result[0].text


@mcp.tool()
async def generate_report(
    group_id: str,
    context: str = None,
    monthly_volume: int = 10000,
    user_id: str = None,
) -> str:
    """
    Generate a PDF report for a completed evaluation.

    Combines LLM-generated narrative (objective analysis) with programmatic
    data tables. The report is saved to disk and can be downloaded from
    the viewer at /api/compare/report/{group_id}.

    Call this right after an evaluation completes, optionally passing the
    conversation context so the narrative reflects what the user was
    trying to evaluate.

    Args:
        group_id: Evaluation run ID (runId from run_evaluation response)
        context: Optional brief description of what the user was evaluating
            and why, used to tailor the report narrative
        monthly_volume: Projected monthly call volume for cost projections (default: 10000)

    Returns:
        JSON with report path and download URL
    """
    args = {
        "user_id": _user(user_id),
        "group_id": group_id,
        "context": context,
        "monthly_volume": monthly_volume,
    }
    result = await handle_generate_report(args)
    return result[0].text


@mcp.tool()
async def explore_eval_data(
    user_id: str = None,
    code: str = "",
) -> str:
    """
    Explore evaluation data by running Python code against eval logs.

    You have access to these functions and the full Inspect AI log API:
    - list_logs() → list of {"file": path, "run_id": id, "task": name, "model": model, "status": status}
    - read_log(file, header_only=False) → EvalLog object
    - read_sample(file, sample_id) → EvalSample object

    Assign your result to the variable `result`.

    Args:
        code: Python code to execute. Assign result to `result` variable.

    Returns:
        The value of `result` as JSON string.
    """
    from inspect_ai.log import read_eval_log, read_eval_log_sample
    from inspect_ai._view.common import list_eval_logs_async
    from eval_mcp.core.user_storage import get_user_log_dir

    if not code:
        return json.dumps({"error": "code is required"})

    log_dir = get_user_log_dir(_user(user_id))

    async def _list_logs():
        from inspect_ai.log import read_eval_log_async
        infos = await list_eval_logs_async(log_dir)
        results = []
        for info in infos[:20]:
            try:
                header = await read_eval_log_async(info.name, header_only=True)
                results.append({
                    "file": info.name,
                    "run_id": header.eval.run_id,
                    "task": header.eval.task,
                    "model": header.eval.model,
                    "status": header.status,
                    "samples": header.eval.dataset.samples if header.eval.dataset else 0,
                })
            except Exception:
                pass
        return results

    import asyncio
    logs_list = await _list_logs()

    def list_logs():
        return logs_list

    def read_log(file, header_only=False):
        return read_eval_log(file, header_only=header_only)

    def read_sample(file, sample_id):
        return read_eval_log_sample(file, id=sample_id)

    try:
        local_vars = {
            "list_logs": list_logs,
            "read_log": read_log,
            "read_sample": read_sample,
            "json": json,
        }
        exec(code, {"__builtins__": __builtins__}, local_vars)
        result = local_vars.get("result", "No 'result' variable set")
        return json.dumps(result, indent=2, default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


# ============================================================
# Entrypoint
# ============================================================

def main():
    """Entry point for eval-mcp CLI."""
    transport = os.environ.get("EVAL_MCP_TRANSPORT", "stdio")

    if transport == "http":
        import uvicorn
        from starlette.routing import Route
        from starlette.responses import JSONResponse

        async def cancel_handler(request):
            user_id = request.path_params["user_id"]
            result = await cancel_user_evaluation(user_id)
            return JSONResponse(result)

        def eval_info_handler(request):
            user_id = request.path_params["user_id"]
            return JSONResponse(get_running_eval_info(user_id))

        app = mcp.streamable_http_app()
        app.routes.insert(0, Route("/eval-info/{user_id}", eval_info_handler, methods=["GET"]))
        app.routes.insert(0, Route("/cancel/{user_id}", cancel_handler, methods=["POST"]))

        print(f"Starting Eval MCP Server on http://{host}:{port}/mcp")
        uvicorn.run(app, host=host, port=port, log_level="info", timeout_graceful_shutdown=30)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
