#!/usr/bin/env python3
"""Manual end-to-end check: optimizer actually IMPROVES, not just doesn't regress.

The repo's integration test only asserts winner >= initial. This script
demonstrates the loop converting an intentionally-broken initial prompt
into one that scores higher, by reading the improvement notes from the
jury and producing a structurally different proposal.

Run manually (costs Bedrock $$):
    .venv/bin/python scripts/verify_optimizer_improves.py
"""

from __future__ import annotations

import asyncio
import os
import tempfile

# Set USER_STORAGE_BASE before importing so user_storage picks it up.
_tmp = tempfile.mkdtemp(prefix="optim_verify_")
os.environ["USER_STORAGE_BASE"] = _tmp

from eval_mcp.core.bedrock_client import BedrockClient
from eval_mcp.core.judge_config import JudgeConfig
from eval_mcp.tools.optimize_prompt import optimize_prompt_loop


# Intentionally broken initial prompt: forces single-word lowercase replies,
# which will fail specificity/directness/length-alignment on every question.
INITIAL_PROMPT = (
    "Answer with exactly one lowercase word and nothing else. "
    "No punctuation, no sentences, no explanation. "
    "Question: {question}"
)


def main() -> int:
    bedrock = BedrockClient()
    judge_config = JudgeConfig(
        criteria=[
            {"name": "specificity",
             "description": "1 if the answer includes specific details, 0 if too vague or too short"},
            {"name": "completeness",
             "description": "1 if the answer covers the same key points as the reference, 0 otherwise"},
            {"name": "format_appropriateness",
             "description": "1 if the answer is written in complete sentences matching the reference style, 0 otherwise"},
        ],
        judges={"claude": "bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0"},
    )
    qa_pairs = [
        {"question": "What's the capital of France?",
         "golden_answer": "Paris is the capital of France, located on the Seine River in northern France."},
        {"question": "Who wrote Hamlet?",
         "golden_answer": "William Shakespeare wrote Hamlet around 1600. It is one of his most famous tragedies."},
        {"question": "Explain photosynthesis briefly.",
         "golden_answer": "Plants use chlorophyll to convert sunlight, water, and CO2 into glucose and oxygen, providing energy for growth."},
        {"question": "What is the Pythagorean theorem?",
         "golden_answer": "For a right triangle, a squared plus b squared equals c squared, where c is the hypotenuse."},
        {"question": "What causes the seasons?",
         "golden_answer": "Earth's axial tilt of about 23.5 degrees relative to its orbit changes which hemisphere receives more direct sunlight throughout the year."},
        {"question": "What is gravity?",
         "golden_answer": "Gravity is the fundamental force of attraction between objects with mass, described by Newton's law and refined by Einstein's general relativity."},
    ]

    print(f"USER_STORAGE_BASE = {_tmp}")
    print(f"Initial prompt: {INITIAL_PROMPT!r}\n")
    print("Running optimizer loop (max_iter=2)...\n")

    out = asyncio.run(
        optimize_prompt_loop(
            bedrock=bedrock,
            user_id="verify",
            optimization_id="opt_verify",
            qa_pairs=qa_pairs,
            judge_config=judge_config,
            providers=["bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0"],
            initial_prompt=INITIAL_PROMPT,
            max_iter=2,
            sample_size=4,
            test_holdout=0.4,
        )
    )

    print("=" * 80)
    print(f"Status: {out['status']}")
    print(f"Train/test split: {out['train_size']} / {out['test_size']}\n")

    print("Per-iteration train scores + eval_run_id:")
    for h in out["history"]:
        print(f"  iter {h['iter']}: train={h['train_pass_rate']:.2f}  "
              f"run_id={h.get('eval_run_id')!r}  "
              f"prompt_snippet={h['prompt'][:80]!r}")

    print(f"\nTest scores by iter: {out['test_scores_by_iter']}")
    print(f"Test run_id: {out.get('test_run_id')}")

    print(f"\nRationales (from optimizer LLM):")
    for k, v in out.get("rationales", {}).items():
        print(f"  iter {k}: {v[:200]}")

    print(f"\nWinner iter {out['winner_iter']}  test score {out['winner_test_score']:.2f}")
    print(f"Winner prompt:\n{out['winner_prompt']}\n")

    initial_test = out["test_scores_by_iter"].get(0, 0.0)
    winner_test = out["winner_test_score"]
    delta = winner_test - initial_test

    print("=" * 80)
    print(f"VERDICT")
    print(f"  Initial test score: {initial_test:.2f}")
    print(f"  Winner  test score: {winner_test:.2f}")
    print(f"  Delta: {delta:+.2f}")
    if delta > 0:
        print("  ✅ Optimizer IMPROVED the prompt end-to-end.")
        return 0
    elif delta == 0 and initial_test == 1.0:
        print("  ✅ Already at ceiling — nothing to improve.")
        return 0
    else:
        print("  ❌ Optimizer did not improve. Investigate prompts/criteria above.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
