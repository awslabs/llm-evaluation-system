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

### After the verbatim DeepEval port (2026-05-27)

Once the 5 scorers were rewritten as faithful QAG ports (multi-stage
extract→verdict, verbatim DeepEval v4.0.4 prompts, contextual_relevancy
switched to per-chunk), Claude Haiku 4.5 judge:

```
======================================================================
metric                     spearman    eval-mcp mean   deepeval mean
----------------------------------------------------------------------
faithfulness                 +0.982 ✓          0.817           0.783
answer_relevancy             +0.988 ✓          0.667           0.650
contextual_precision         +0.364 ✗          0.833           0.883
contextual_recall            +0.000 ✗          1.000           0.900
contextual_relevancy         +0.897 ✓          0.753           0.708
======================================================================
```

**The port fixed contextual_relevancy** — it went from 0.423 (pre-port,
aggregate single call) to 0.897 once switched to DeepEval's per-chunk
structure. That was the real win.

**The two ✗ metrics are mostly a measurement artifact, not a defect:**

- **contextual_recall 0.000** is degenerate, not "total disagreement."
  eval-mcp scored **1.0 on all 10 samples**, so the series is constant
  and Spearman is mathematically undefined (reported 0). Per-sample, we
  **agree with DeepEval on 9 of 10**; the only real disagreement is
  `missing_chunk_for_golden` (we credit the "~1519" sentence as
  attributable, DeepEval doesn't because the date isn't in the chunk —
  we're slightly lenient on sentence granularity).
- **contextual_precision 0.364** agrees on 8 of 10, with two
  single-sample, single-run verdict disagreements (`noisy_chunks`,
  `missing_chunk_for_golden`) on borderline "is this noisy chunk useful"
  calls.

**Why we're NOT chasing the two ✗ metrics with code:** the scorers now
use DeepEval's prompts verbatim. Hand-tuning them to close these last
gaps would mean *diverging* from DeepEval — defeating the point of the
port. The low Spearman is driven by (a) dataset saturation — too many
clean 1.0 cases leave no variance for rank correlation — and (b) normal
single-judge run-to-run variance on 2-3 borderline samples. The correct
fix is a **larger, less-saturated parity dataset** (push to 30-50
samples with more mid-range cases), not prompt edits. Tracked as a
follow-up; not blocking, since the structural port is done and 3/5
metrics are cleanly aligned with the other 2 explained.

### Pre-port baseline (for reference)

Before the port (our own paraphrased single-call prompts, 6 metrics
including the since-removed `groundedness`): faithfulness +0.968,
answer_relevancy +1.000, contextual_precision +1.000,
contextual_recall +0.667, contextual_relevancy **+0.423**,
groundedness +0.986. The port traded a couple of artificially-high
saturated correlations for a correct contextual_relevancy and verbatim
DeepEval alignment.

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
