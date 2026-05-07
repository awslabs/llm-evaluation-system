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

from mcp.server import FastMCP

from backend.core.bedrock_client import BedrockClient, create_boto3_bedrock_client
from backend.core.user_storage import list_user_document_paths
from backend.mcp_servers.dataset.agent import DatasetAgent
from backend.mcp_servers.dataset.tools.save_dataset import handle_save_dataset
from backend.mcp_servers.providers.external_providers import (
    detect_available_providers,
    get_external_models,
)
from backend.mcp_servers.synthetic.tools.generate_qa_pairs import handle_generate_qa_pairs
from backend.mcp_servers.synthetic.tools.generate_judge import handle_generate_judge
from backend.mcp_servers.synthetic.tools.create_eval_config import handle_create_eval_config
from backend.mcp_servers.synthetic.tools.create_agent_eval_config import handle_create_agent_eval_config
from backend.mcp_servers.synthetic.tools.analyze_agent_image import handle_analyze_agent_image
from backend.mcp_servers.synthetic.tools.list_datasets import handle_list_datasets
from backend.mcp_servers.synthetic.tools.list_judges import handle_list_judges
from backend.mcp_servers.synthetic.tools.list_evaluations import handle_list_evaluations
from backend.mcp_servers.synthetic.tools.get_evaluation_details import handle_get_evaluation_details
from backend.mcp_servers.synthetic.tools.run_evaluation import (
    handle_run_evaluation,
    handle_retry_evaluation,
    cancel_user_evaluation,
    get_running_eval_info,
)

# Configuration
region = os.environ.get("AWS_REGION", "us-west-2")
port = int(os.environ.get("EVAL_MCP_PORT", "8002"))
host = os.environ.get("HOST", "127.0.0.1")

# Initialize server
mcp = FastMCP("eval-server", port=port, host=host)

# Shared clients
bedrock = BedrockClient(region=region)


# ============================================================
# Dataset tools
# ============================================================

@mcp.tool()
async def analyze_dataset(
    file_content: str,
    filename: str = "dataset.csv",
    user_id: str = None,
) -> str:
    """
    Analyze a CSV dataset for structure and quality.

    Uses an intelligent agent to parse the CSV, detect structure,
    identify question/answer columns, and check for data quality issues.

    Args:
        file_content: The raw CSV file content as a string
        filename: Name of the file (for display purposes)

    Returns:
        JSON analysis report with validity, column mapping, issues, and summary
    """
    agent = DatasetAgent(bedrock)
    analysis = await agent.analyze(file_content, filename)
    return json.dumps({"success": True, "filename": filename, "analysis": analysis}, indent=2)


@mcp.tool()
async def save_dataset(
    file_content: str,
    filename: str,
    column_mapping: dict,
    user_id: str = None,
) -> str:
    """
    Save a CSV dataset for evaluation.

    Converts the CSV to the required format with question and golden_answer fields.

    Args:
        file_content: The raw CSV file content
        filename: Original filename (used for naming the output)
        column_mapping: Dict with 'question' and 'golden_answer' keys mapping to CSV column names

    Returns:
        JSON with success status, path, and rows saved
    """
    args = {
        "file_content": file_content,
        "filename": filename,
        "column_mapping": column_mapping,
        "user_id": user_id,
    }
    result = await handle_save_dataset(args)
    return result[0].text


# ============================================================
# Provider/model discovery tools
# ============================================================

# Import supported models list from providers module
from backend.mcp_servers.providers.server_http import SUPPORTED_MODELS


@mcp.tool()
def list_bedrock_models(
    provider: str = "all",
    limit: int = 0,
    text_only: bool = True,
) -> str:
    """
    Get list of AWS Bedrock models available for evaluations.

    Queries both inference profiles (cross-region) and foundation models to return
    all models you have access to. Returns models with correct format (bedrock/*) ready to use.

    Args:
        provider: Filter by provider name (case-insensitive):
            - "all" (default): All providers
            - "anthropic", "meta", "mistral", "amazon", "deepseek", "nvidia", etc.
        limit: Maximum number of models to return (default: 0 = unlimited)
        text_only: If True (default), only return text generation models

    Returns:
        JSON list of available model IDs in bedrock/* format
    """
    try:
        client = create_boto3_bedrock_client("bedrock", region)
        available = []

        # Check inference profiles
        try:
            response = client.list_inference_profiles(maxResults=100, typeEquals="SYSTEM_DEFINED")
            for profile in response.get("inferenceProfileSummaries", []):
                model_id = profile.get("inferenceProfileId", "")
                full_id = f"bedrock/{model_id}"
                available.append(full_id)
        except Exception:
            pass

        # Check foundation models
        try:
            params = {}
            if provider != "all":
                params["byProvider"] = provider
            if text_only:
                params["byOutputModality"] = "TEXT"
            response = client.list_foundation_models(**params)
            for model in response.get("modelSummaries", []):
                model_id = model.get("modelId", "")
                full_id = f"bedrock/{model_id}"
                if full_id in SUPPORTED_MODELS and full_id not in available:
                    available.append(full_id)
        except Exception:
            pass

        # Filter by provider
        if provider != "all":
            provider_lower = provider.lower()
            available = [m for m in available if provider_lower in m.lower()]

        if limit > 0:
            available = available[:limit]

        return json.dumps({"models": available, "count": len(available)}, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e), "models": []})


@mcp.tool()
def list_available_models(
    provider: str = "all",
    source: str = "all",
) -> str:
    """
    List all models available for evaluations, across Bedrock and external providers.

    Combines AWS Bedrock models with any external providers that have API keys configured
    (OpenAI, Anthropic direct, Google Gemini, etc.).

    Args:
        provider: Filter by provider name (case-insensitive):
            - "all" (default): All providers
            - "openai": OpenAI models (requires OPENAI_API_KEY)
            - "anthropic": Anthropic models (Bedrock + direct API if key set)
            - "google": Google Gemini models (requires GOOGLE_API_KEY)
            - Or any Bedrock provider name (amazon, meta, mistral, etc.)

        source: Filter by source:
            - "all" (default): Bedrock + external providers
            - "bedrock": Only AWS Bedrock models
            - "external": Only external provider models (OpenAI, Anthropic direct, Google, etc.)

    Returns:
        JSON with available models from all configured providers
    """
    all_models = []

    if source in ("all", "bedrock"):
        try:
            bedrock_result = json.loads(list_bedrock_models(provider=provider))
            if "models" in bedrock_result:
                for m in bedrock_result["models"]:
                    all_models.append({"id": m, "source": "bedrock"})
        except Exception:
            pass

    if source in ("all", "external"):
        external = get_external_models(provider=provider)
        for m in external:
            m["source"] = "external"
            all_models.extend([m] if isinstance(m, dict) else [{"id": m, "source": "external"}])

    available_providers = detect_available_providers()

    if not all_models:
        return json.dumps({
            "models": [],
            "count": 0,
            "available_providers": available_providers,
            "note": "No models found. Check AWS credentials for Bedrock, or configure API keys for external providers.",
        })

    return json.dumps({
        "models": all_models,
        "count": len(all_models),
        "available_providers": available_providers,
    }, indent=2)


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
        "user_id": user_id,
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
    if not user_id:
        return json.dumps({"success": False, "error": "user_id is required"})
    try:
        paths = list_user_document_paths(user_id)
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
        "user_id": user_id,
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
    configName: str = "evaluation",
    description: str = None,
    judge_models: list = None,
) -> str:
    """
    Create an Inspect AI evaluation configuration with multi-judge support.

    Generates config with LLM judges that evaluate using binary scores.
    Results are aggregated by Jury scoring.

    Args:
        dataset: Name of dataset from list_datasets
        providers: List of target models to evaluate. Supports multiple provider formats:
            - Bedrock: "bedrock/us.anthropic.claude-sonnet-4-6"
            - OpenAI: "openai/gpt-4o" (requires OPENAI_API_KEY)
            - Anthropic direct: "anthropic/claude-sonnet-4-6" (requires ANTHROPIC_API_KEY)
            - Google: "google/gemini-2.5-pro" (requires GOOGLE_API_KEY)
            Use list_available_models() to discover available providers and models.
        judge: Name of judge from list_judges (REQUIRED - criteria adapted to QA pairs)
        prompts: Single prompt string OR list of prompts for comparison. Use {question} or {prompt} as placeholder for the input text. (default: "{question}")
        configName: Name for this evaluation (default: "evaluation")
        description: Optional description of the evaluation
        judge_models: Optional list of model IDs to use as judges

    Returns:
        JSON with config path and summary
    """
    args = {
        "dataset": dataset,
        "providers": providers,
        "judge": judge,
        "user_id": user_id,
        "prompts": prompts,
        "configName": configName,
        "description": description,
        "judge_models": judge_models,
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
    configName: str = "agent_evaluation",
    description: str = None,
    judge_models: list = None,
) -> str:
    """
    Create an evaluation config for testing an agent running in a container.

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
        configName: Name for this evaluation (default: "agent_evaluation")
        description: Optional description of the evaluation
        judge_models: Optional list of model IDs to use as judges

    Returns:
        JSON with config path and summary
    """
    args = {
        "dataset": dataset,
        "judge": judge,
        "agentImage": agentImage,
        "user_id": user_id,
        "agentCmd": agentCmd or ["python", "agent.py"],
        "model": model,
        "configName": configName,
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
    configName: str = "agent_evaluation",
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
        configName: Name for this evaluation (default: "agent_evaluation")
        context: Optional user description of what the agent should do

    Returns:
        JSON with eval config ready to run, including analysis summary
    """
    args = {
        "agentImage": agentImage,
        "user_id": user_id,
        "numSamples": numSamples,
        "agentCmd": agentCmd,
        "model": model,
        "configName": configName,
        "context": context,
    }
    result = await handle_analyze_agent_image(args)
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
    args = {"user_id": user_id, "searchTerm": searchTerm}
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
    args = {"user_id": user_id, "searchTerm": searchTerm}
    result = await handle_list_judges(args)
    return result[0].text


@mcp.tool()
async def list_evaluations(
    user_id: str = None,
    limit: int = 20,
) -> str:
    """
    List completed evaluations.

    Returns a list of previous evaluation runs with IDs, descriptions, and timestamps.

    Args:
        limit: Maximum number of evaluations to return (default: 20)

    Returns:
        JSON with list of evaluations and their metadata
    """
    args = {"user_id": user_id, "limit": limit}
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
    args = {"evalId": evalId, "user_id": user_id}
    result = await handle_get_evaluation_details(args)
    return result[0].text


@mcp.tool()
async def run_evaluation(
    configName: str,
    user_id: str = None,
    maxConcurrency: int = 4,
) -> str:
    """
    Run an evaluation with automatic jury multi-judge scoring.

    Configs created by create_eval_config include scoring logic that
    automatically computes jury scores from multiple LLM judges.

    Flow:
    1. Runs target model(s) via Inspect AI
    2. Each response evaluated by judges
    3. Results written to .eval log files for viewing

    Args:
        configName: Name of the evaluation config from create_eval_config
        maxConcurrency: Maximum concurrent model requests (default: 4)

    Returns:
        JSON with evaluation results including scores
    """
    args = {
        "configName": configName,
        "user_id": user_id,
        "maxConcurrency": maxConcurrency,
    }
    result = await handle_run_evaluation(args)
    return result[0].text


@mcp.tool()
async def retry_evaluation(
    user_id: str = None,
    maxConcurrency: int = 16,
) -> str:
    """
    Retry failed or incomplete evaluations.

    Finds evaluations that failed or were interrupted and retries only
    the incomplete samples.

    Args:
        maxConcurrency: Maximum concurrent model requests (default: 16)

    Returns:
        JSON with retry results
    """
    args = {"user_id": user_id, "maxConcurrency": maxConcurrency}
    result = await handle_retry_evaluation(args)
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
    from backend.core.user_storage import get_user_log_dir

    if not user_id:
        return json.dumps({"error": "user_id is required"})
    if not code:
        return json.dumps({"error": "code is required"})

    log_dir = get_user_log_dir(user_id)

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

if __name__ == "__main__":
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
