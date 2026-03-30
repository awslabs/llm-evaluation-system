"""Tool schema builder for Jury multi-judge evaluation.

Generates Claude tool_use schemas dynamically from JudgeConfig criteria.
Using tool_use ensures reliable JSON output matching the schema.
"""

from typing import Any, Dict

from backend.core.judge_config import JudgeConfig


def build_judge_tool_schema(config: JudgeConfig | None = None) -> Dict[str, Any]:
    """Build tool schema from configurable criteria.

    Each criterion becomes a property with:
    - score: integer (0 or 1)
    - reason: string (brief explanation)

    Using tool_use with forced tool_choice ensures the model outputs
    valid JSON matching the schema, avoiding parsing errors.

    Args:
        config: JudgeConfig instance. If None, uses default config.

    Returns:
        Tool schema dict ready for Claude tool_use API
    """
    config = config or JudgeConfig()

    properties: Dict[str, Any] = {}
    for criterion in config.criteria:
        properties[criterion["name"]] = {
            "type": "object",
            "properties": {
                "score": {
                    "type": "integer",
                    "enum": [0, 1],
                    "description": criterion["description"],
                },
                "reason": {
                    "type": "string",
                    "description": "Brief explanation (1-2 sentences)",
                },
            },
            "required": ["score", "reason"],
        }

    return {
        "name": "submit_judgment",
        "description": (
            "Submit your evaluation judgment. You MUST call this tool "
            "with your assessment for each criterion."
        ),
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": config.criteria_names,
        },
    }


def get_default_judge_tool() -> Dict[str, Any]:
    """Get tool schema with default 4 criteria."""
    return build_judge_tool_schema(JudgeConfig())


def parse_judgment_response(response: Dict[str, Any]) -> Dict[str, Any]:
    """Parse judgment from Claude tool_use response.

    Args:
        response: Claude API response dict

    Returns:
        Judgment dict with scores and reasons for each criterion

    Raises:
        ValueError: If no tool_use found in response
    """
    content = response.get("content", [])

    for block in content:
        if block.get("type") == "tool_use" and block.get("name") == "submit_judgment":
            return block.get("input", {})

    raise ValueError("No submit_judgment tool_use found in response")


def extract_scores_only(judgment: Dict[str, Any]) -> Dict[str, int]:
    """Extract just the binary scores from a judgment.

    Args:
        judgment: Parsed judgment dict with scores and reasons

    Returns:
        Dict mapping criterion names to scores (0 or 1)
    """
    scores = {}
    for criterion_name, data in judgment.items():
        if isinstance(data, dict) and "score" in data:
            scores[criterion_name] = data["score"]
        elif isinstance(data, int):
            scores[criterion_name] = data
    return scores


def extract_reasons_only(judgment: Dict[str, Any]) -> Dict[str, str]:
    """Extract just the reasons from a judgment.

    Args:
        judgment: Parsed judgment dict with scores and reasons

    Returns:
        Dict mapping criterion names to reason strings
    """
    reasons = {}
    for criterion_name, data in judgment.items():
        if isinstance(data, dict) and "reason" in data:
            reasons[criterion_name] = data["reason"]
        elif isinstance(data, str):
            reasons[criterion_name] = data
    return reasons
