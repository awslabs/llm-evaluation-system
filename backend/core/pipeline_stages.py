"""Data model for multi-stage agent evaluation pipelines.

Each stage represents a different aspect of agent behavior that gets its own
scorer. Stages are ordered and executed sequentially during evaluation.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class PipelineStage:
    """A single evaluation stage in a multi-stage pipeline.

    Args:
        name: Snake_case identifier for the stage.
        display_name: Human-readable label shown in the UI.
        order: Execution order (lower runs first).
        scorer_type: Either "deterministic" or "llm_judge".
        criteria: For llm_judge stages only. Each dict has "name" and
            "description" keys defining a scoring criterion.
        check: For deterministic stages. The type of check to perform,
            e.g. "tool_called", "includes_text".
        expected_field: Name of the dataset metadata field that holds the
            expected value for this stage.
        context_filter: Which portion of the agent transcript to evaluate.
            One of "all", "first_response", "tool_calls_only", "final_output".
    """

    name: str
    display_name: str
    order: int
    scorer_type: str
    criteria: list[dict[str, str]] | None = None
    check: str | None = None
    expected_field: str | None = None
    context_filter: str = "all"


@dataclass
class PipelineConfig:
    """Ordered collection of pipeline stages for an evaluation run.

    Args:
        stages: The list of PipelineStage objects comprising this pipeline.
    """

    stages: list[PipelineStage] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the pipeline config to a plain dict."""
        return {"stages": [asdict(stage) for stage in self.stages]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PipelineConfig:
        """Deserialize a pipeline config from a plain dict."""
        stages = [PipelineStage(**stage_data) for stage_data in data["stages"]]
        return cls(stages=stages)

    @classmethod
    def default_for_agent(cls) -> PipelineConfig:
        """Return a sensible two-stage default pipeline for agent evaluation.

        Stage 1 -- tool_selection: deterministic check that the agent called
        the expected tools.

        Stage 2 -- final_output: LLM judge scoring the agent's final answer
        for correctness and completeness.
        """
        return cls(
            stages=[
                PipelineStage(
                    name="tool_selection",
                    display_name="Tool Selection",
                    order=1,
                    scorer_type="deterministic",
                    check="tool_called",
                    expected_field="expected_tools",
                    context_filter="tool_calls_only",
                ),
                PipelineStage(
                    name="final_output",
                    display_name="Final Output",
                    order=2,
                    scorer_type="llm_judge",
                    criteria=[
                        {
                            "name": "output_correctness",
                            "description": "The agent's final answer is factually correct and directly addresses the user's request.",
                        },
                        {
                            "name": "completeness",
                            "description": "The response covers all required aspects of the task without omitting important details.",
                        },
                    ],
                    context_filter="final_output",
                ),
            ]
        )
