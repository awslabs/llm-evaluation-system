"""Jury scoring function for promptfoo assertScoringFunction.

Called by promptfoo after all judge assertions complete.
Uses hierarchical voting (two-stage majority):
  1. For each criterion, majority vote across judges
  2. Pass overall if majority of criteria pass

This addresses the issue with flat MAV where dissenting votes on failed
criteria could push a response over the threshold. Hierarchical voting
ensures each criterion is treated as an independent pass/fail test.

Usage in promptfoo config:
  defaultTest:
    assertScoringFunction: file://backend/core/jury_scoring.py:compute_jury_score
"""

from typing import Any, Dict, List


def decode_binary_score(score: int | float, num_criteria: int) -> List[int]:
    """Decode binary scores from integer format (e.g., 10101).

    Args:
        score: Integer encoding binary scores (each digit is 0 or 1)
        num_criteria: Expected number of criteria

    Returns:
        List of binary scores [1, 0, 1, 0, 1]

    Raises:
        ValueError: If score is invalid
    """
    if score is None:
        raise ValueError("FATAL: Score is None - judge failed to return a valid score")

    if not isinstance(score, (int, float)):
        raise ValueError(f"FATAL: Score must be a number, got {type(score).__name__}: {score}")

    if score < 0:
        raise ValueError(f"FATAL: Score must be non-negative, got {score}")

    # Convert to string and zero-pad to expected length
    digits = str(int(score)).zfill(num_criteria)

    # Validate digit count matches exactly (after zero-padding, should be exactly num_criteria)
    if len(digits) > num_criteria:
        raise ValueError(
            f"FATAL: Score {score} has {len(digits)} digits but expected {num_criteria}. "
            f"Judge returned wrong format."
        )

    # Validate each digit is 0 or 1
    for i, digit in enumerate(digits):
        if digit not in '01':
            raise ValueError(
                f"FATAL: Invalid digit '{digit}' at position {i+1} in score {score}. "
                f"Expected 0 or 1."
            )

    return [int(d) for d in digits]


def format_binary_score(score: int | float, num_criteria: int) -> str:
    """Format binary score as string with proper padding.

    Args:
        score: Integer encoding binary scores
        num_criteria: Expected number of criteria

    Returns:
        Formatted string like "10101"
    """
    return str(int(score)).zfill(num_criteria)


def compute_jury_score(
    named_scores: Dict[str, float],
    context: Dict[str, Any] = None,
) -> Dict[str, Any]:
    """Compute jury score using hierarchical voting.

    Two-stage majority voting:
      1. For each criterion, majority vote across judges (pass if > 50% of judges agree)
      2. Pass overall if majority of criteria pass

    This is the assertScoringFunction called by promptfoo.

    Args:
        named_scores: Dict of named scores (contains judge_xxx metrics)
        context: Dict with componentResults (individual judge GradingResults)

    Returns:
        GradingResult dict with jury score and formatted reasoning
    """
    context = context or {}
    component_results = context.get("componentResults", [])

    if not component_results:
        raise ValueError("FATAL: No judge results found in componentResults")

    # Get num_criteria and criteria_names from first assertion's config
    num_criteria = None
    criteria_names = None
    for result in component_results:
        assertion = result.get("assertion", {})
        config = assertion.get("config", {})
        if "num_criteria" in config:
            num_criteria = config["num_criteria"]
            criteria_names = config.get("criteria_names")
            break

    if num_criteria is None:
        raise ValueError(
            "FATAL: num_criteria not found in assertion config. "
            "Config must be created with create_eval_config which adds num_criteria to each assertion."
        )

    # Fallback to c1, c2, etc. if names not provided (backward compatibility)
    if not criteria_names:
        criteria_names = [f"c{i+1}" for i in range(num_criteria)]

    # Collect votes per criterion across all judges
    # votes_by_criterion[i] = list of votes from all judges for criterion i
    votes_by_criterion: List[List[int]] = [[] for _ in range(num_criteria)]
    judge_outputs = []

    for result in component_results:
        score = result.get("score")
        reason = result.get("reason", "No reason provided")

        # Get judge identifier from assertion metric (e.g., "judge_claude" -> "claude")
        assertion = result.get("assertion", {})
        metric = assertion.get("metric", "")
        if metric.startswith("judge_"):
            judge_name = metric[6:]  # Remove "judge_" prefix
        else:
            judge_name = metric or f"judge_{len(judge_outputs) + 1}"

        if score is None:
            raise ValueError(f"FATAL: Judge '{judge_name}' returned no score")

        # Decode and validate - will raise on any issue
        votes = decode_binary_score(score, num_criteria)

        # Collect votes per criterion
        for i, vote in enumerate(votes):
            votes_by_criterion[i].append(vote)

        score_str = format_binary_score(score, num_criteria)
        votes_passed = sum(votes)
        judge_outputs.append(
            f"**{judge_name}** ({score_str}) - {votes_passed}/{num_criteria}: {reason}"
        )

    n_judges = len(component_results)

    # Stage 1: For each criterion, compute majority across judges
    criteria_results = []
    for i, criterion_votes in enumerate(votes_by_criterion):
        votes_for = sum(criterion_votes)
        criterion_avg = votes_for / len(criterion_votes)
        criterion_passed = criterion_avg > 0.5
        criteria_results.append({
            "name": criteria_names[i],
            "votes_for": votes_for,
            "avg": criterion_avg,
            "passed": criterion_passed,
        })

    # Stage 2: Count how many criteria passed
    criteria_passed_count = sum(1 for c in criteria_results if c["passed"])
    jury_score = criteria_passed_count / num_criteria
    passed = jury_score > 0.5

    # Build formatted reason
    verdict = "PASS" if passed else "FAIL"

    # Criteria breakdown
    criteria_breakdown = []
    for c in criteria_results:
        emoji = "✓" if c["passed"] else "✗"
        criteria_breakdown.append(
            f"  {emoji} {c['name']}: {c['votes_for']}/{n_judges} judges ({c['avg']:.2f})"
        )

    header = (
        f"**Jury: {jury_score:.2f} ({verdict})** - "
        f"{criteria_passed_count}/{num_criteria} criteria passed "
        f"(hierarchical voting, {n_judges} judges)"
    )
    criteria_section = "\n".join(criteria_breakdown)
    judges_section = "\n\n".join(judge_outputs)

    reason = header + "\n\n" + criteria_section + "\n\n---\n\n" + judges_section

    # Build namedScores with per-criterion breakdown
    # Each criterion shows proportion of judges who passed it
    named_scores = {}
    for c in criteria_results:
        named_scores[c["name"]] = c["avg"]

    return {
        "pass": passed,
        "score": jury_score,
        "reason": reason,
        "namedScores": named_scores,
        "assertScoringFunctionUsed": True,  # Signal UI to show only final pass/fail
    }
