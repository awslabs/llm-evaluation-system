"""Generate custom evaluation criteria for Jury multi-judge evaluation."""

import asyncio
import json
from typing import Any, Dict, List

from mcp.types import TextContent

from eval_mcp.core.bedrock_client import BedrockClient
from eval_mcp.core.user_storage import save_judge_to_db, get_dataset_by_name
from eval_mcp.core.judge_config import MAX_CRITERIA, DEFAULT_CRITERIA

# Tool schema for structured criteria output
CRITERIA_TOOL = {
    "name": "submit_criteria",
    "description": "Submit the evaluation criteria you've designed.",
    "input_schema": {
        "type": "object",
        "properties": {
            "criteria": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Short snake_case name for the criterion (e.g., factual_accuracy)"
                        },
                        "description": {
                            "type": "string",
                            "description": "Binary description: '1 if [condition], 0 otherwise'"
                        }
                    },
                    "required": ["name", "description"]
                },
                "minItems": 3,
                "maxItems": 5,
                "description": "Evaluation criteria for judging responses (around 5 ideal, up to 10)"
            }
        },
        "required": ["criteria"]
    }
}


async def generate_judge(
    bedrock: BedrockClient,
    qa_pairs: List[Dict[str, str]],
    domain: str,
) -> Dict[str, Any]:
    """Generate custom evaluation criteria based on QA pairs.

    Analyzes up to 10 QA pairs to create domain-specific binary criteria
    for Jury multi-judge evaluation.

    Args:
        bedrock: Bedrock client
        qa_pairs: List of {"question": "...", "golden_answer": "..."} dicts
        domain: Domain/purpose of the evaluation

    Returns:
        Dict with "criteria" list for JudgeConfig
    """
    # Format default criteria as examples
    default_examples = "\n".join([
        f"  - {c['name']}: {c['description']}" for c in DEFAULT_CRITERIA
    ])

    system_prompt = f"""You are an expert at designing evaluation criteria for LLM outputs.
Your task is to analyze question-answer pairs and create binary (0 or 1) evaluation criteria.

Guidelines:
- Create criteria that capture what matters for this domain (around 5 is ideal, up to 10 if needed)
- Each criterion must be binary: "1 if [specific condition], 0 otherwise"
- Focus on what actually matters based on the QA examples
- Use snake_case for criterion names (e.g., factual_accuracy, completeness)
- Make criteria objective and clear for multiple judges to apply consistently
- Responses will be compared against a reference (golden) answer — criteria should reflect closeness to the reference

Here are example criteria to use as inspiration (adapt to the domain, don't copy verbatim):
{default_examples}"""

    # Format QA pairs for analysis (up to 10)
    qa_examples = "\n\n".join([
        f"Q{i+1}: {qa['question']}\nA{i+1}: {qa['golden_answer']}"
        for i, qa in enumerate(qa_pairs[:10])
    ])

    user_prompt = f"""Analyze these question-answer pairs from a {domain} evaluation:

<QA_Examples>
{qa_examples}
</QA_Examples>

Based on these examples, design binary evaluation criteria that capture what makes a good response in this domain (around 5 is ideal, up to 10 if needed).

Consider:
- What factual elements must be correct?
- What level of completeness is expected?
- Are there format/structure requirements evident in the golden answers?
- What domain-specific qualities matter?

Call the submit_criteria tool with your criteria."""

    messages = [{"role": "user", "content": user_prompt}]

    response = await asyncio.to_thread(
        bedrock.create_message,
        messages=messages,
        system=system_prompt,
        tools=[CRITERIA_TOOL],
        tool_choice={"type": "auto"},
        max_tokens=2048,
        temperature=0,  # Deterministic output for reproducible criteria
    )

    # Extract tool use result
    for block in response.get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == "submit_criteria":
            return block.get("input", {})

    # Fallback if no tool use found
    raise ValueError("Failed to generate structured criteria")


async def handle_generate_judge(
    bedrock: BedrockClient, args: Dict[str, Any]
) -> List[TextContent]:
    """Handle generate_judge tool call.

    Generates custom evaluation criteria based on QA pairs for Jury multi-judge.

    Args:
        bedrock: Bedrock client instance
        args: Tool arguments containing dataset (name), domain, and user_id

    Returns:
        MCP TextContent response with judge_id and name
    """
    dataset_name = args.get("dataset")
    domain = args.get("domain", "general")
    user_id = args.get("user_id")

    # Validate required args
    if not user_id:
        return [
            TextContent(
                type="text",
                text=json.dumps({"success": False, "error": "user_id is required"}),
            )
        ]
    if not dataset_name:
        return [
            TextContent(
                type="text",
                text=json.dumps({
                    "success": False,
                    "error": "dataset is required - use list_datasets to see available datasets",
                }),
            )
        ]

    try:
        # Load dataset from database
        dataset_data = get_dataset_by_name(user_id, dataset_name)
        if not dataset_data:
            return [
                TextContent(
                    type="text",
                    text=json.dumps({
                        "success": False,
                        "error": f"Dataset '{dataset_name}' not found. Use list_datasets to see available datasets.",
                    }),
                )
            ]

        tests = dataset_data.get("tests", [])

        # Convert test cases to qa_pairs format
        qa_pairs = [
            {"question": tc["vars"]["question"], "golden_answer": tc["vars"]["golden_answer"]}
            for tc in tests
        ]

        if not qa_pairs:
            return [
                TextContent(
                    type="text",
                    text=json.dumps({
                        "success": False,
                        "error": "No QA pairs provided",
                    }),
                )
            ]

        # Generate criteria (analyzes up to 10 QA pairs)
        criteria_result = await generate_judge(bedrock, qa_pairs, domain)
        criteria = criteria_result.get("criteria", [])

        if not criteria:
            return [
                TextContent(
                    type="text",
                    text=json.dumps({
                        "success": False,
                        "error": "Failed to generate criteria",
                    }),
                )
            ]

        # Save to user's database
        safe_name = "".join(c if c.isalnum() or c in (' ', '-', '_') else '_' for c in domain[:50])
        safe_name = safe_name.strip().replace(' ', '_').lower()
        judge_name = f"{safe_name}_criteria"

        judge_config = {
            "domain": domain,
            "criteria": criteria,
            "samples_analyzed": min(len(qa_pairs), 10),
        }
        judge_id = save_judge_to_db(user_id, judge_name, judge_config)

        # Format criteria for display
        criteria_preview = "\n".join([
            f"  - {c['name']}: {c['description']}"
            for c in criteria
        ])

        result = {
            "success": True,
            "judge_id": judge_id,
            "name": judge_name,
            "domain": domain,
            "criteria_count": len(criteria),
            "criteria": criteria_preview,
            "samples_analyzed": min(len(qa_pairs), 10),
        }

        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    except Exception as e:
        return [
            TextContent(
                type="text",
                text=json.dumps({
                    "success": False,
                    "error": f"Failed to generate criteria: {str(e)}",
                }),
            )
        ]
