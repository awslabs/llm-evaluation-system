"""Judge prompt builder for Jury multi-judge evaluation.

Generates judge prompts dynamically from JudgeConfig criteria.
Prompts instruct judges to output binary scores (0 or 1) for each criterion.
"""

from eval_mcp.core.judge_config import JudgeConfig


JUDGE_PROMPT_TEMPLATE = """Evaluate the AI answer against the reference. For each criterion, provide a binary score (0 or 1) and a brief reason.

Scoring Rules:
- Score 1 = criterion is satisfied
- Score 0 = criterion is NOT satisfied
- Be strict and objective
- Do not inflate scores

Criteria:
{criteria_list}

[Question]
{question}

[AI Answer]
{output}

[Reference Gold Answer]
{golden_answer}

Call the submit_judgment tool with your assessment for each criterion."""


def build_judge_prompt(
    question: str,
    output: str,
    golden_answer: str,
    config: JudgeConfig | None = None,
) -> str:
    """Build judge prompt from configurable criteria.

    Args:
        question: The original question
        output: The AI's response to evaluate
        golden_answer: The reference/gold answer
        config: JudgeConfig instance. If None, uses default config.

    Returns:
        Formatted prompt string ready for judge model
    """
    config = config or JudgeConfig()

    criteria_lines = []
    for criterion in config.criteria:
        criteria_lines.append(f"- {criterion['name']}: {criterion['description']}")

    criteria_list = "\n".join(criteria_lines)

    return JUDGE_PROMPT_TEMPLATE.format(
        criteria_list=criteria_list,
        question=question,
        output=output,
        golden_answer=golden_answer,
    )


def build_judge_prompt_template(config: JudgeConfig | None = None) -> str:
    """Build judge prompt template with placeholders.

    Returns a template with {question}, {output}, {golden_answer} placeholders.
    Useful for saving templates to files.

    Args:
        config: JudgeConfig instance. If None, uses default config.

    Returns:
        Template string with placeholders
    """
    config = config or JudgeConfig()

    criteria_lines = []
    for criterion in config.criteria:
        criteria_lines.append(f"- {criterion['name']}: {criterion['description']}")

    criteria_list = "\n".join(criteria_lines)

    return JUDGE_PROMPT_TEMPLATE.format(
        criteria_list=criteria_list,
        question="{question}",
        output="{output}",
        golden_answer="{golden_answer}",
    )


def save_judge_prompt_template(config: JudgeConfig, output_path: str) -> None:
    """Save generated prompt template to file.

    Args:
        config: JudgeConfig to generate template from
        output_path: Path to save the template
    """
    template = build_judge_prompt_template(config)
    with open(output_path, "w") as f:
        f.write(template)
