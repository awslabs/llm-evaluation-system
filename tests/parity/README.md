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

## Results

### Sonnet 4.6 judge — the representative run (2026-05-27)

This is the run that matters: **Sonnet 4.6 is the product's default
judge** (`judge_config.JUDGE_MODELS["claude"]`), and the RAG QAG metrics
are single-judge (no voting), so judge quality is decisive.

```
======================================================================
metric                     spearman    eval-mcp mean   deepeval mean
----------------------------------------------------------------------
faithfulness                 +1.000 ✓          0.783           0.783
answer_relevancy             +1.000 ✓          0.733           0.733
contextual_precision         +1.000 ✓          0.883           0.883
contextual_recall            +0.667 ~          0.900           0.800
contextual_relevancy         +0.875 ✓          0.720           0.678
======================================================================
```

**On a capable judge, the port matches DeepEval almost exactly** — three
metrics at perfect 1.000 rank agreement. The only soft spot is
contextual_recall (0.667), driven by a single borderline sentence-
attribution call (`missing_chunk_for_golden`: we credit "~1519" as
attributable, DeepEval doesn't because the date isn't in the chunk).
That's a genuine edge case, not a judge artifact — and we leave it,
because the prompt is verbatim DeepEval; tuning it would diverge from
the reference.

### Haiku 4.5 judge — DON'T use for RAG scorers

The same suite on Haiku 4.5 looked alarming: contextual_precision 0.364,
contextual_recall 0.000. **Those were the judge, not our code.** Example:
on `noisy_chunks`, Haiku wrongly marked the answer-containing chunk as
irrelevant (precision 0.0); Sonnet gets it right (1.0, matching DeepEval).

Lesson baked into the default: **single-judge QAG metrics need a strong
judge.** Haiku is too weak — its verdicts on borderline relevance/
attribution calls are unreliable. The RAG scorers pick up whatever judge
the jury is configured with, which defaults to Sonnet 4.6. Don't drop
the RAG judge to Haiku to save cost — the metric quality collapses.

| metric | Haiku 4.5 | Sonnet 4.6 |
|---|---|---|
| faithfulness | 0.982 | **1.000** |
| answer_relevancy | 0.988 | **1.000** |
| contextual_precision | 0.364 ✗ | **1.000** |
| contextual_recall | 0.000 ✗ | 0.667 |
| contextual_relevancy | 0.897 | 0.875 |

### Pre-port baseline (for reference)

Before the verbatim port (our paraphrased single-call prompts, 6 metrics
including the since-removed `groundedness`, Haiku judge): contextual_
relevancy was **0.423** — the metric was structurally wrong (single
aggregate call vs DeepEval's per-chunk). The port fixed that, and moving
to Sonnet 4.6 cleaned up the rest.

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
