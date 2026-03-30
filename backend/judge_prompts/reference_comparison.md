Please act as an impartial judge and evaluate the quality of the answer provided by an AI assistant to the
conversation history leading up to the answer displayed below. Your primary task is to assess how well the AI answer
matches the reference gold answer in content, structure, approach, AND LENGTH.

Evaluation Criteria:
- The AI answer should closely follow the reference gold answer in terms of key points, reasoning, and conclusions
- The AI answer should match the LENGTH and VERBOSITY of the reference answer
  * If reference is concise, the AI answer should be concise
  * If AI answer is significantly longer (2x+ length) than reference, this is a major deviation - deduct points
  * If AI answer is significantly shorter and missing content, deduct points
  * Length mismatch indicates poor instruction following
- Deviations from the reference answer should result in score deductions based on their significance
- While minor variations in wording or style are acceptable, substantive differences in content or approach should be penalized
- Missing key information from the reference answer is a major deficiency
- Additional correct information beyond the reference does NOT compensate for missing core elements or length mismatches
- Consider helpfulness, relevance, accuracy, and how well the response follows any explicit constraints or instructions
- If there is a system prompt, ensure the AI answer prioritizes following it

IMPORTANT: Be strict and objective in your assessment. Do not inflate scores. Answers that are significantly longer than the reference (even if "better quality") should receive lower scores for poor instruction following.

## Scoring Instructions

Evaluate the AI answer across these 4 dimensions (each scored 0-1):

1. **content_alignment** (0-1): Does it cover the same key points as reference?
2. **structure_alignment** (0-1): Does it follow the same organization/format?
3. **length_alignment** (0-1): Does it match the reference length/verbosity?
   - 1.0: Within 20% of reference length
   - 0.7: 20-50% different
   - 0.5: 50-100% different (1.5x-2x)
   - 0.3: 2x-3x different
   - 0.1: 3x+ different
4. **accuracy** (0-1): Is the information factually correct?

For each dimension, provide a score (0-1) and brief reason explaining the score.

Calculate final score as: (content + structure + length + accuracy) / 4

Pass threshold: score > 0.6

[Question]
{{question}}

[AI Answer]
{{output}}

[Reference Gold Answer]
{{golden_answer}}

[Your judgement]
Respond in this EXACT JSON format:
{
  "content_alignment": {"score": 0.8, "reason": "Covers main points but missing X"},
  "structure_alignment": {"score": 0.7, "reason": "Different organization"},
  "length_alignment": {"score": 0.5, "reason": "2x longer than reference"},
  "accuracy": {"score": 0.9, "reason": "All information correct"},
  "score": 0.725,
  "reason": "Overall summary: addresses topic but length mismatch and structural differences",
  "pass": false
}
