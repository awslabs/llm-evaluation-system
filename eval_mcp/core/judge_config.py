"""Configuration for Jury multi-judge evaluation.

Defines configurable judges (LLM models) and criteria (evaluation dimensions).
Used by create_eval_config.py to generate Inspect AI eval configs with multi-judge
scoring.
"""

from typing import Dict, List


# Judge models for multi-judge evaluation
# Model IDs use Inspect AI provider format (bedrock/ prefix)
JUDGE_MODELS: Dict[str, str] = {
    "claude": "bedrock/us.anthropic.claude-sonnet-4-6",
    "nova": "bedrock/us.amazon.nova-pro-v1:0",
    "nemotron": "bedrock/nvidia.nemotron-super-3-120b",
}

# Default criteria for evaluation - binary (0 or 1)
DEFAULT_CRITERIA: List[Dict[str, str]] = [
    {
        "name": "content_alignment",
        "description": "1 if covers same key points as reference, 0 otherwise",
    },
    {
        "name": "structure_alignment",
        "description": "1 if follows similar organization/format, 0 otherwise",
    },
    {
        "name": "length_alignment",
        "description": "1 if within 50% of reference length, 0 otherwise",
    },
    {
        "name": "accuracy",
        "description": "1 if factually correct, 0 otherwise",
    },
]

# Maximum criteria allowed. The critic loop in generate_judge.py picks
# the final count within this ceiling — most domains converge around 6–10.
MAX_CRITERIA = 15

# Minimum responses for reliable scoring
MIN_RESPONSES_FOR_JURY = 50


class JudgeConfig:
    """Configurable judge settings for Jury evaluation.

    Supports dynamic configuration of judges (LLM models) and criteria
    (evaluation dimensions). Both tool schemas and prompts are generated
    from this configuration.

    Attributes:
        criteria: List of criterion definitions with name and description
        judges: Dict mapping judge labels to model IDs

    Example:
        # Default configuration (4 judges x 4 criteria = 16 signals)
        config = JudgeConfig()

        # Custom criteria
        config = JudgeConfig(criteria=LEGAL_CRITERIA)

        # Subset of judges
        config = JudgeConfig(judges={"claude": JUDGE_MODELS["claude"]})
    """

    def __init__(
        self,
        criteria: List[Dict[str, str]] | None = None,
        judges: Dict[str, str] | None = None,
    ) -> None:
        """Initialize judge configuration.

        Args:
            criteria: List of criteria, each with 'name' and 'description' keys.
                      Defaults to DEFAULT_CRITERIA.
            judges: Dict of judge labels to model IDs.
                    Defaults to JUDGE_MODELS.

        Raises:
            ValueError: If more than MAX_CRITERIA criteria are specified.
            ValueError: If criteria are missing required keys.
        """
        self.criteria = criteria or DEFAULT_CRITERIA
        self.judges = judges or JUDGE_MODELS

        # Validate criteria count
        if len(self.criteria) > MAX_CRITERIA:
            raise ValueError(
                f"Maximum {MAX_CRITERIA} criteria allowed, got {len(self.criteria)}"
            )

        # Validate criteria structure
        for criterion in self.criteria:
            if "name" not in criterion:
                raise ValueError(f"Criterion missing 'name': {criterion}")
            if "description" not in criterion:
                raise ValueError(f"Criterion missing 'description': {criterion}")

    @property
    def criteria_names(self) -> List[str]:
        """Get list of criterion names."""
        return [c["name"] for c in self.criteria]

    @property
    def num_signals(self) -> int:
        """Total signals = judges x criteria."""
        return len(self.judges) * len(self.criteria)

    def get_criteria_description(self, name: str) -> str:
        """Get description for a criterion by name.

        Args:
            name: Criterion name to look up

        Returns:
            Description string, or empty string if not found
        """
        for c in self.criteria:
            if c["name"] == name:
                return c["description"]
        return ""

    def to_dict(self) -> Dict:
        """Serialize config to dictionary for storage."""
        return {
            "criteria": self.criteria,
            "judges": self.judges,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "JudgeConfig":
        """Deserialize config from dictionary.

        Args:
            data: Dictionary with 'criteria' and 'judges' keys

        Returns:
            JudgeConfig instance
        """
        return cls(
            criteria=data.get("criteria"),
            judges=data.get("judges"),
        )


# Example: Custom criteria for legal document evaluation
LEGAL_CRITERIA: List[Dict[str, str]] = [
    {"name": "legal_accuracy", "description": "1 if legally accurate, 0 otherwise"},
    {"name": "citation_quality", "description": "1 if citations are correct, 0 otherwise"},
    {"name": "argument_structure", "description": "1 if argument is well-structured, 0 otherwise"},
    {"name": "completeness", "description": "1 if addresses all aspects, 0 otherwise"},
    {"name": "clarity", "description": "1 if clearly written, 0 otherwise"},
]

# Example: Custom criteria for code review evaluation
CODE_REVIEW_CRITERIA: List[Dict[str, str]] = [
    {"name": "correctness", "description": "1 if code solution is correct, 0 otherwise"},
    {"name": "efficiency", "description": "1 if solution is efficient, 0 otherwise"},
    {"name": "readability", "description": "1 if code is readable, 0 otherwise"},
    {"name": "completeness", "description": "1 if handles edge cases, 0 otherwise"},
]
