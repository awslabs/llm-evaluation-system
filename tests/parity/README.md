# DeepEval parity test

Compares `eval_mcp`'s 6 RAG scorers against DeepEval's equivalents on a
10-sample reference dataset, using the **same Bedrock judge model** for
both systems. Reports per-metric Spearman rank correlation.

## Why this exists

Our RAG scorers diverge from DeepEval in three documented, intentional
ways (see `eval_mcp/scorers/rag.py` docstring):

1. **One judge call per metric** vs DeepEval's two-stage extract-then-verdict.
2. **Groundedness** (1 − contradiction rate, higher = better) instead of
   DeepEval's raw `hallucination_rate`.
3. **One context field** (`retrieval_context`) instead of DeepEval's
   `context` + `retrieval_context` split.

These should not affect *ranking* — the sample DeepEval considers more
faithful should be the sample we consider more faithful. The 2026 paper
[*Rethinking Atomic Decomposition for LLM Judges*](https://arxiv.org/pdf/2603.28005)
shows holistic (single-call) and atomic decomposition agree on 2 of 3
QA benchmarks. We expect the same agreement here.

**Pass threshold:** Spearman ≥ 0.7 per metric. 0.5–0.7 is a yellow flag
(re-investigate prompts). < 0.5 means we've diverged from DeepEval in
a way that needs attention.

## Why this is NOT part of pytest

Running this script makes ~160 Bedrock calls (10 samples × 6 metrics ×
~1.5 calls per metric across both systems). At Claude Haiku 4.5 pricing
that's a few cents per run, but it's slow (a few minutes) and burns
budget. CI would charge that on every PR.

Run it on demand when you've changed scorer prompts, aggregation logic,
or want to re-verify parity. Results cache in `.parity_cache.json` so
reruns are free.

## Setup

```bash
# DeepEval + boto3 aren't pinned in pyproject — install ad-hoc.
.venv/bin/pip install deepeval boto3

# Verify AWS Bedrock access from the venv:
.venv/bin/python -c "import boto3; print(boto3.client('bedrock-runtime', region_name='us-west-2').meta.region_name)"
```

## Running

```bash
BEDROCK_MODEL_ID="us.anthropic.claude-haiku-4-5-20251001-v1:0" \
AWS_REGION=us-west-2 \
.venv/bin/python tests/parity/run_parity.py
```

The default model id matches what `list_bedrock_models` would surface
for the cheap Haiku-class judge. Override via `BEDROCK_MODEL_ID` to
test with stronger judges (Sonnet, Opus).

## Actual results (2026-05-27)

First run, Claude Haiku 4.5 judge, 10-sample reference dataset:

```
======================================================================
metric                     spearman    eval-mcp mean   deepeval mean
----------------------------------------------------------------------
faithfulness                  +0.968 ✓        0.733           0.783
answer_relevancy              +1.000 ✓        0.633           0.667
contextual_precision          +1.000 ✓        0.883           0.883
contextual_recall             +0.667 ~        0.900           0.900
contextual_relevancy          +0.423 ✗        0.703           0.753
groundedness                  +0.986 ✓        0.733           0.700
======================================================================
```

5 of 6 metrics show strong rank agreement (≥ 0.7); ``answer_relevancy``
and ``contextual_precision`` show perfect (1.000) agreement.
``contextual_recall`` lands at 0.667 — a single-sample disagreement on
the ``empty_answer`` edge case (we score 0.5, DeepEval scores 1.0)
because we still credit the chunk's facts as "covered" even when the
answer doesn't use them. Defensible either way.

**``contextual_relevancy`` (0.423) is the actionable finding.** Looking
at the per-sample table, the disagreement clusters on samples where the
chunk is clear and on-topic — DeepEval scores 1.00 ("everything in the
chunk is relevant"), we score 0.50 ("only the first half is directly
relevant"). Our statement-extraction prompt is too granular: it pulls
multiple atomic statements per chunk and judges each against the
question, while DeepEval extracts at a coarser, answer-relevant level.

Both are reasonable interpretations of the metric, but they diverge on
clean-chunk cases. To bring this into agreement, tighten the system
prompt in ``contextual_relevancy`` to "extract only the statements that
*could* answer the question" rather than "extract every standalone
statement." That's a follow-up.

## Interpreting output

- **Spearman column** is rank correlation. `✓` ≥ 0.7, `~` 0.5–0.7, `✗` < 0.5.
- **Means** are absolute averages across the 10 samples. Useful for
  sanity-checking that our absolute scores are in the same ballpark —
  they should be close but not identical (single-call vs two-stage
  produces different absolute values).

## Editing the reference dataset

`reference_dataset.json` has 10 hand-crafted samples covering known
behaviors (perfect grounding, contradiction, ranking quality, missing
chunks, off-topic answer, noisy chunks, partial answer, fabrication,
empty answer, multi-chunk synthesis). Each sample has an
`expected_direction` block — informal labels, not asserted, but useful
to spot when a metric scored a sample wildly different from what the
sample was designed to test.

Add new samples freely. Spearman correlation stabilises around 10
samples for binary-ish metrics; for more confidence on borderline
cases, push toward 20.
