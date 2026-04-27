#!/usr/bin/env python3
"""
Synthetic Eval MCP Server - HTTP implementation.

Provides AI-powered test question generation using AWS Bedrock over HTTP.
Uses official MCP Python SDK with FastMCP.
"""

import importlib.util
import json
import os

from mcp.server import FastMCP
from backend.core.bedrock_client import BedrockClient
from backend.core.user_storage import list_user_document_paths

# DISABLED: generate_questions (moved to old_tools - confuses LLM with similar functionality to generate_qa_pairs)
# Use generate_qa_pairs instead for creating test datasets with golden answers

# Import generate_qa_pairs
spec_qa = importlib.util.spec_from_file_location(
    "generate_qa_pairs",
    os.path.join(os.path.dirname(__file__), "tools", "generate_qa_pairs.py")
)
generate_qa_pairs_module = importlib.util.module_from_spec(spec_qa)
spec_qa.loader.exec_module(generate_qa_pairs_module)
handle_generate_qa_pairs = generate_qa_pairs_module.handle_generate_qa_pairs

# Import generate_judge
spec_judge = importlib.util.spec_from_file_location(
    "generate_judge",
    os.path.join(os.path.dirname(__file__), "tools", "generate_judge.py")
)
generate_judge_module = importlib.util.module_from_spec(spec_judge)
spec_judge.loader.exec_module(generate_judge_module)
handle_generate_judge = generate_judge_module.handle_generate_judge

# Import create_eval_config
spec_config = importlib.util.spec_from_file_location(
    "create_eval_config",
    os.path.join(os.path.dirname(__file__), "tools", "create_eval_config.py")
)
create_config_module = importlib.util.module_from_spec(spec_config)
spec_config.loader.exec_module(create_config_module)
handle_create_eval_config = create_config_module.handle_create_eval_config

# DISABLED: list_eval_configs (configs are ephemeral, not worth cataloging)
# Judges and datasets are reusable assets; configs are just execution plumbing

# Import list_datasets
spec_list_datasets = importlib.util.spec_from_file_location(
    "list_datasets",
    os.path.join(os.path.dirname(__file__), "tools", "list_datasets.py")
)
list_datasets_module = importlib.util.module_from_spec(spec_list_datasets)
spec_list_datasets.loader.exec_module(list_datasets_module)
handle_list_datasets = list_datasets_module.handle_list_datasets

# Import list_judges
spec_list_judges = importlib.util.spec_from_file_location(
    "list_judges",
    os.path.join(os.path.dirname(__file__), "tools", "list_judges.py")
)
list_judges_module = importlib.util.module_from_spec(spec_list_judges)
spec_list_judges.loader.exec_module(list_judges_module)
handle_list_judges = list_judges_module.handle_list_judges

# Import run_evaluation and cancel function
spec_run_eval = importlib.util.spec_from_file_location(
    "run_evaluation",
    os.path.join(os.path.dirname(__file__), "tools", "run_evaluation.py")
)
run_eval_module = importlib.util.module_from_spec(spec_run_eval)
spec_run_eval.loader.exec_module(run_eval_module)
handle_run_evaluation = run_eval_module.handle_run_evaluation
cancel_user_evaluation = run_eval_module.cancel_user_evaluation
get_running_eval_info = run_eval_module.get_running_eval_info

# Import list_evaluations
spec_list_evals = importlib.util.spec_from_file_location(
    "list_evaluations",
    os.path.join(os.path.dirname(__file__), "tools", "list_evaluations.py")
)
list_evals_module = importlib.util.module_from_spec(spec_list_evals)
spec_list_evals.loader.exec_module(list_evals_module)
handle_list_evaluations = list_evals_module.handle_list_evaluations

# Import get_evaluation_details
spec_get_eval = importlib.util.spec_from_file_location(
    "get_evaluation_details",
    os.path.join(os.path.dirname(__file__), "tools", "get_evaluation_details.py")
)
get_eval_module = importlib.util.module_from_spec(spec_get_eval)
spec_get_eval.loader.exec_module(get_eval_module)
handle_get_evaluation_details = get_eval_module.handle_get_evaluation_details


# Get configuration
region = os.environ.get("AWS_REGION", "us-west-2")
port = int(os.environ.get("SYNTHETIC_EVAL_MCP_SERVER_PORT", "8002"))
host = os.environ.get("HOST", "127.0.0.1")

# Initialize FastMCP server with port and host
mcp = FastMCP("synthetic-eval-server", port=port, host=host)

# Initialize Bedrock client
bedrock = BedrockClient(region=region)


# DISABLED: generate_questions tool (confuses LLM - use generate_qa_pairs instead)
# @mcp.tool()
# async def generate_questions(...):
#     ...


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
async def list_documents(
    user_id: str = None,
) -> str:
    """
    List all uploaded documents available for the user.

    Use this to discover existing documents that can be used with generate_qa_pairs.
    Returns document paths that can be passed to generate_qa_pairs(documents=[...]).

    Returns:
        JSON with list of document paths
    """
    if not user_id:
        return json.dumps({
            "success": False,
            "error": "user_id is required",
        })

    try:
        paths = list_user_document_paths(user_id)
        return json.dumps({
            "success": True,
            "documents": paths,
            "count": len(paths),
            "hint": "Pass these paths to generate_qa_pairs(documents=[...]) to generate QA pairs from documents",
        })
    except Exception as e:
        return json.dumps({
            "success": False,
            "error": str(e),
        })


@mcp.tool()
async def generate_judge(
    dataset: str,
    user_id: str = None,
    domain: str = "general",
) -> str:
    """
    Generate a custom LLM judge from a dataset.

    Analyzes up to 10 QA pairs from the dataset to create domain-specific
    binary evaluation criteria for multi-judge scoring.

    Args:
        dataset: Name of dataset from list_datasets
        domain: Domain description (e.g., "healthcare", "customer support")

    Returns:
        JSON with judge_id and name
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
    prompts: str | list = "{{question}}",
    configName: str = "evaluation",
    description: str = None,
    judge_models: list = None,
) -> str:
    """
    Create an Inspect AI evaluation configuration with multi-judge support.

    Generates config with LLM judges that evaluate using binary scores encoded
    as integer format (e.g., 10101). Results are aggregated by Jury scoring.

    Args:
        dataset: Name of dataset from list_datasets
        providers: List of target models to evaluate. Supports multiple provider formats:
            - Bedrock: "bedrock/us.anthropic.claude-sonnet-4-6"
            - OpenAI: "openai/gpt-4o" (requires OPENAI_API_KEY)
            - Anthropic direct: "anthropic/claude-sonnet-4-6" (requires ANTHROPIC_API_KEY)
            - Google: "google/gemini-2.5-pro" (requires GOOGLE_API_KEY)
            Use list_available_models() to discover available providers and models.
        judge: Name of judge from list_judges (REQUIRED - criteria adapted to QA pairs)
        prompts: Single prompt string OR list of prompts (default: "{{question}}")
        configName: Name for this evaluation (default: "evaluation")
        description: Optional description of the evaluation
        judge_models: Optional list of Bedrock model IDs to use as judges
            (e.g., ["bedrock/us.anthropic.claude-sonnet-4-6", "bedrock/deepseek.r1-v1:0"])
            Default: Claude, Nova, and Llama

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


# DISABLED: list_eval_configs tool (configs are ephemeral, not worth cataloging)
# @mcp.tool()
# async def list_eval_configs(...)


@mcp.tool()
async def list_datasets(
    user_id: str = None,
    searchTerm: str = None,
) -> str:
    """
    List available datasets.

    Returns details about each dataset including number of samples and preview of first question.
    Dataset names can be used with generate_judge and create_eval_config.

    Args:
        searchTerm: Optional search term to filter datasets by name

    Returns:
        Formatted list of datasets with details
    """
    args = {
        "user_id": user_id,
        "searchTerm": searchTerm,
    }
    result = await handle_list_datasets(args)
    return result[0].text


@mcp.tool()
async def list_judges(
    user_id: str = None,
    searchTerm: str = None,
) -> str:
    """
    List available LLM judges.

    Returns details about each judge including domain and preview of evaluation criteria.
    Judge names can be used with create_eval_config.

    Args:
        searchTerm: Optional search term to filter judges by name

    Returns:
        Formatted list of judges with details
    """
    args = {
        "user_id": user_id,
        "searchTerm": searchTerm,
    }
    result = await handle_list_judges(args)
    return result[0].text




@mcp.tool()
async def list_evaluations(
    user_id: str = None,
    limit: int = 20,
) -> str:
    """
    List completed evaluations from the database.

    Returns a list of previous evaluation runs with their IDs, descriptions, and timestamps.
    Use get_evaluation_details with an evaluation ID to see full results.

    Args:
        limit: Maximum number of evaluations to return (default: 20)

    Returns:
        JSON with list of evaluations and their metadata
    """
    args = {
        "user_id": user_id,
        "limit": limit,
    }
    result = await handle_list_evaluations(args)
    return result[0].text


@mcp.tool()
async def get_evaluation_details(
    evalId: str,
    user_id: str = None,
) -> str:
    """
    Get detailed results for a specific evaluation.

    Returns the full results including individual test outcomes, scores, and grading details.
    Use list_evaluations first to find the evaluation ID.

    Args:
        evalId: The evaluation ID to retrieve (from list_evaluations)

    Returns:
        JSON with detailed evaluation results including summary stats and individual test results
    """
    args = {
        "evalId": evalId,
        "user_id": user_id,
    }
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
async def get_viewer_url(
    user_id: str = None,
    eval_id: str = None,
) -> str:
    """
    Get the URL for viewing evaluation results.

    Returns the viewer URL for the user's evaluation results.
    The viewer is managed by the backend and shows all evaluations for this user.

    Args:
        user_id: The user ID to get the viewer URL for.
        eval_id: Optional evaluation ID to deep-link to a specific eval.
            If not provided, returns the URL showing all evaluations.

    Returns:
        URL to view evaluation results (e.g., /viewer/{user_id}/eval/{eval_id})
    """
    if not user_id:
        return json.dumps({
            "success": False,
            "error": "user_id is required",
        })

    base_url = os.environ.get("APP_URL", "http://localhost:4001")
    viewer_url = f"{base_url}/results"

    return json.dumps({
        "success": True,
        "url": viewer_url,
        "message": f"View evaluation results at {viewer_url}",
    })


if __name__ == "__main__":
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

    # Get MCP app and add routes
    app = mcp.streamable_http_app()
    app.routes.insert(0, Route("/eval-info/{user_id}", eval_info_handler, methods=["GET"]))
    app.routes.insert(0, Route("/cancel/{user_id}", cancel_handler, methods=["POST"]))

    print(f"✓ Starting Synthetic Eval MCP Server on http://{host}:{port}/mcp")
    uvicorn.run(app, host=host, port=port, log_level="info", timeout_graceful_shutdown=30)
