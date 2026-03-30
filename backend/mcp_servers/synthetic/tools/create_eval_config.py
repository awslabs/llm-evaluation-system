"""Create promptfoo evaluation configuration with multi-judge jury scoring.

Generates configs that use multiple LLM judges via llm-rubric assertions.
Each judge outputs binary scores encoded in the score field (integer format (e.g., 10101)).
Results aggregated by jury_scoring.py (MAV simple mean) via assertScoringFunction.

All evaluations use jury scoring - this is mandatory.
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from mcp.types import TextContent

from backend.core.judge_config import JudgeConfig
from backend.core.user_storage import (
    get_judge_by_name,
    get_dataset_by_name,
    get_user_dir,
)

# Path to jury scoring function (computed at module load)
_JURY_SCORING_PATH = Path(__file__).parent.parent.parent.parent / "core" / "jury_scoring.py"


def build_judge_rubric_prompt(criteria: List[Dict[str, str]]) -> str:
    """Build the judge rubric prompt from criteria.

    Args:
        criteria: List of criteria dicts with 'name' and 'description'

    Returns:
        Rubric prompt string for llm-rubric
    """
    # Build criteria descriptions
    criteria_lines = "\n".join([
        f"{i+1}. {c['name']}: {c['description']}"
        for i, c in enumerate(criteria)
    ])

    # Build encoding example based on criteria count
    num_criteria = len(criteria)
    example_encoding = "1" * num_criteria  # e.g., "11111" for 5 criteria
    zero_example = "0" * num_criteria  # e.g., "00000" for all fail

    return f"""You are a judge evaluating an AI answer against a reference answer. Score each criterion as BINARY (0 or 1).

<criteria>
{criteria_lines}
</criteria>

<question>
{{{{question}}}}
</question>

<ai_answer>
{{{{output}}}}
</ai_answer>

<reference_answer>
{{{{golden_answer}}}}
</reference_answer>

<scoring_instructions>
Encode scores as an integer where each digit represents one criterion (in order):
- Digit 1 = criterion 1, Digit 2 = criterion 2, etc.
- All pass: score = {example_encoding}
- All fail: score = {zero_example}
- Mixed example: scores [1,0,1,0] -> score = 1010
</scoring_instructions>

<output_format>
{{
  "pass": true,
  "score": {example_encoding},
  "reason": "Brief explanation of scores"
}}
</output_format>

Your response must be ONLY the JSON object above. Do not include any other text, explanation, or commentary outside the JSON.
Do not use LaTeX notation (like \\(x\\) or $x$) in your reason - use plain text only."""


def create_promptfoo_config(
    dataset_path: str,
    providers: List[str],
    prompts: str | List[str],
    config_name: str,
    config_dir: str,
    judge_config: JudgeConfig,
    description: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a promptfoo configuration with multi-judge jury evaluation.

    All evaluations use jury scoring via assertScoringFunction.
    Each judge is an llm-rubric assertion with a metric tag.
    The jury_scoring.py function computes the final score from all judges.

    Args:
        dataset_path: Absolute path to dataset file
        providers: List of provider strings (target models to evaluate)
        prompts: Single prompt string or list of prompts
        config_name: Name for this evaluation
        config_dir: Directory where config will be saved
        judge_config: JudgeConfig with criteria and judges (required)
        description: Optional description

    Returns:
        Promptfoo config dict with multi-judge llm-rubric assertions and jury scoring
    """
    # Calculate relative path from config dir to dataset
    config_dir_path = Path(config_dir)
    dataset_path_obj = Path(dataset_path)
    relative_dataset = os.path.relpath(dataset_path_obj, config_dir_path)

    # Normalize prompts to list
    prompts_list = [prompts] if isinstance(prompts, str) else prompts

    # Build rubric prompt from criteria
    rubric_prompt = build_judge_rubric_prompt(judge_config.criteria)

    # Helper to convert provider to Converse API format with temperature=0
    def to_converse_provider(provider: str) -> dict:
        # Convert bedrock:model to bedrock:converse:model for unified API
        if provider.startswith("bedrock:") and not provider.startswith("bedrock:converse:"):
            provider = provider.replace("bedrock:", "bedrock:converse:", 1)
        # Don't set maxTokens - let each model use its default limit
        # (Llama models have 2048 limit, Claude has 8192+, etc.)
        return {"id": provider, "config": {"temperature": 0}}

    # Create llm-rubric assertion for each judge with metric tag
    # Include criteria info in config so scoring function can use names
    # Use temperature=0 for deterministic, reproducible judge outputs
    num_criteria = len(judge_config.criteria)
    criteria_names = [c["name"] for c in judge_config.criteria]
    assertions = [
        {
            "type": "llm-rubric",
            "provider": to_converse_provider(model_id),
            "metric": f"judge_{label}",
            "config": {"num_criteria": num_criteria, "criteria_names": criteria_names},
        }
        for label, model_id in judge_config.judges.items()
    ]

    # Get absolute path to jury scoring function
    jury_scoring_path = _JURY_SCORING_PATH.resolve()

    # Convert provider strings to use Converse API (reuse helper from above)
    providers_with_config = [to_converse_provider(p) for p in providers]

    config = {
        "description": description or f"Evaluation: {config_name}",
        "providers": providers_with_config,
        "prompts": prompts_list,
        "tests": f"file://{relative_dataset}",
        "defaultTest": {
            "options": {
                "rubricPrompt": rubric_prompt,
            },
            "assert": assertions,
            "assertScoringFunction": f"file://{jury_scoring_path}:compute_jury_score",
        },
    }

    return config


async def handle_create_eval_config(args: Dict[str, Any]) -> List[TextContent]:
    """Handle create_eval_config tool call.

    Args:
        args: Tool arguments containing:
            - dataset: Name of dataset (from list_datasets)
            - judge: Name of judge (from list_judges)
            - providers: List of model providers to evaluate
            - prompts: Prompt template(s) (default: "{{question}}")
            - configName: Name for this config (default: "evaluation")
            - description: Optional description
            - user_id: User ID

    Returns:
        MCP TextContent response with configName
    """
    try:
        # Extract arguments
        dataset_name = args.get("dataset")
        judge_name = args.get("judge")
        providers = args.get("providers")
        prompts = args.get("prompts", "{{question}}")
        config_name = args.get("configName", "evaluation")
        description = args.get("description")
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
                    text=json.dumps({"success": False, "error": "dataset is required - use list_datasets to see available datasets"}),
                )
            ]
        if not judge_name:
            return [
                TextContent(
                    type="text",
                    text=json.dumps({
                        "success": False,
                        "error": "judge is required - use list_judges to see available judges, or generate_judge to create one",
                    }),
                )
            ]
        if not providers:
            return [
                TextContent(
                    type="text",
                    text=json.dumps({
                        "success": False,
                        "error": "At least one provider is required",
                    }),
                )
            ]

        # Validate prompt count (prevent massive eval costs)
        MAX_PROMPTS = 50
        prompts_list = [prompts] if isinstance(prompts, str) else prompts
        if len(prompts_list) > MAX_PROMPTS:
            return [
                TextContent(
                    type="text",
                    text=json.dumps({
                        "success": False,
                        "error": f"Maximum {MAX_PROMPTS} prompts allowed per evaluation, got {len(prompts_list)}",
                    }),
                )
            ]

        # Load judge from database
        judge_data = get_judge_by_name(user_id, judge_name)
        if not judge_data:
            return [
                TextContent(
                    type="text",
                    text=json.dumps({
                        "success": False,
                        "error": f"Judge '{judge_name}' not found. Use list_judges to see available judges.",
                    }),
                )
            ]

        criteria = judge_data["config"].get("criteria")
        if not criteria:
            return [
                TextContent(
                    type="text",
                    text=json.dumps({
                        "success": False,
                        "error": f"Judge '{judge_name}' has no criteria",
                    }),
                )
            ]
        # Build judge config with optional custom judge models
        judge_models_arg = args.get("judge_models")
        custom_judges = None
        if judge_models_arg:
            custom_judges = {m: m for m in judge_models_arg}
        judge_config = JudgeConfig(criteria=criteria, judges=custom_judges)

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
        if not tests:
            return [
                TextContent(
                    type="text",
                    text=json.dumps({
                        "success": False,
                        "error": f"Dataset '{dataset_name}' is empty",
                    }),
                )
            ]

        # Write dataset to temp file (promptfoo needs a file path)
        user_dir = get_user_dir(user_id)
        temp_dir = user_dir / "temp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        dataset_file = temp_dir / f"{dataset_name}.yaml"
        with open(dataset_file, "w") as f:
            yaml.dump(tests, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

        # Config saved to configs/ directory (run_evaluation constructs path from name)
        config_dir = user_dir / "configs"
        config_dir.mkdir(parents=True, exist_ok=True)
        output_path = config_dir / f"{config_name}.yaml"

        # Create config
        config = create_promptfoo_config(
            dataset_path=str(dataset_file),
            providers=providers,
            prompts=prompts,
            config_name=config_name,
            config_dir=str(config_dir),
            description=description,
            judge_config=judge_config,
        )

        # Save config to file
        with open(output_path, "w") as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)

        # Prepare summary - return configName (not path) for security
        result = {
            "success": True,
            "configName": config_name,
            "summary": {
                "dataset": dataset_name,
                "judge": judge_name,
                "providers": len(providers),
                "prompts": len(prompts_list),
                "testCases": len(tests),
                "description": description or f"Evaluation: {config_name}",
            },
            "nextStep": f"Run evaluation: run_evaluation(configName='{config_name}')",
        }

        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    except Exception as e:
        return [
            TextContent(
                type="text",
                text=json.dumps({
                    "success": False,
                    "error": f"Failed to create config: {str(e)}",
                }),
            )
        ]
