# GPT-5.6 Luna (Bedrock) vs GPT-4o-mini (OpenAI) — aiwf comparison

A worked comparison of two target models on the bundled **aiwf** 30-turn
multi-turn benchmark, produced with the scripts in this directory. It doubles as
a reference for how to read the benchmark's outputs (pass_rate, per-dimension
counters, latency) and how far you can trust a small-sample model comparison.

**Reproduce:**

```bash
# quality, single fixed judge (10 repeats)
OPENAI_API_KEY=sk-... AWS_REGION=us-east-2 python compare_models.py \
  --models openai/bedrock/gpt-5.6-luna openai/gpt-4o-mini \
  --repeats 10 --judge bedrock/us.anthropic.claude-opus-5 --user-prefix cmp10

# quality, jury (Opus 5 + GPT-5.6 Sol, majority vote)
OPENAI_API_KEY=sk-... AWS_REGION=us-east-2 python compare_models.py \
  --models openai/bedrock/gpt-5.6-luna openai/gpt-4o-mini \
  --repeats 10 --jury \
  --judge bedrock/us.anthropic.claude-opus-5 openai/bedrock/gpt-5.6-sol \
  --user-prefix cmpjury

# latency (true TTFT, streaming)
OPENAI_API_KEY=sk-... AWS_REGION=us-east-2 python ttft_probe.py
```

The numbers below are from one measured session (dates/machine noted per
section). They are illustrative — rerun for current figures; model behaviour and
network conditions drift.

---

## 1. Quality — single fixed judge (Opus 5), 10 repeats

Both models saw the identical 30-turn conversation each repeat; the same single
judge (`claude-opus-5`) graded both. Per-repeat `pass_rate`:

| rep | Luna | mini |
|----:|-----:|-----:|
| 1 | 1.000 | 0.867 |
| 2 | 0.867 | 0.833 |
| 3 | 0.833 | 0.867 |
| 4 | 0.900 | 0.833 |
| 5 | 0.867 | 0.800 |
| 6 | 0.933 | 0.833 |
| 7 | 0.867 | 0.867 |
| 8 | 0.900 | 0.867 |
| 9 | 1.000 | 0.833 |
| 10 | 0.800 | 0.867 |

| metric | GPT-5.6 Luna | GPT-4o-mini |
|---|---|---|
| pass_rate mean | **0.897** | 0.847 |
| pass_rate std | 0.066 | **0.023** |
| range | 0.800 – 1.000 | 0.800 – 0.867 |
| tool_use_correct | 27.3 / 30 | 26.8 / 30 |
| instruction_following | 26.9 / 30 | 25.8 / 30 |
| kb_grounding | 30.0 / 30 | 29.6 / 30 |
| median latency* | 0.99 s | 1.89 s |
| recovery nudges (mean) | 2.0 | 3.2 |
| truncated / unjudged turns | 0 / 0 | 0 / 0 |

**Paired difference (Luna − mini):** mean **+0.050**, sd 0.072, t = 2.18, df = 9,
two-sided **p = 0.0571**.

\* Benchmark latency is total generate time (non-streaming), an upper bound on
TTFT — see §3 for the real TTFT measurement.

---

## 2. Quality — jury (Opus 5 + GPT-5.6 Sol), 9 repeats

Same setup, but a two-judge majority-vote jury. **Jury scores are NOT comparable
to the single-judge numbers in §1** (a different, slightly more lenient measuring
instrument); only the *gap between the two target models* carries across.

One repeat (rep 2) aborted before running: GPT-5.6 Sol hit a transient Mantle
validation error, and the fail-fast judge check (added so a bad judge doesn't
cost a full 30-turn run) correctly stopped that repeat. 9 of 10 completed.

Per-repeat `pass_rate`:

| rep | Luna | mini |
|----:|-----:|-----:|
| 1 | 0.933 | 0.900 |
| 3 | 0.967 | 0.900 |
| 4 | 0.867 | 0.900 |
| 5 | 0.967 | 0.867 |
| 6 | 0.967 | 0.867 |
| 7 | 1.000 | 0.833 |
| 8 | 1.000 | 0.833 |
| 9 | 0.833 | 0.900 |
| 10 | 1.000 | 0.900 |

| metric | GPT-5.6 Luna | GPT-4o-mini |
|---|---|---|
| pass_rate mean | **0.948** | 0.878 |
| pass_rate std | 0.060 | **0.029** |
| range | 0.833 – 1.000 | 0.833 – 0.900 |
| tool_use_correct | 28.7 / 30 | 27.6 / 30 |
| instruction_following | 28.4 / 30 | 26.6 / 30 |
| kb_grounding | 30.0 / 30 | 29.7 / 30 |
| median latency | 1.15 s | 1.87 s |
| recovery nudges (mean) | 1.0 | 2.4 |

**Paired difference (Luna − mini):** mean **+0.070**, sd 0.081, t = 2.61, df = 8,
two-sided **p = 0.0309**.

The jury tightened the measurement (two judges agreeing → less noise), moving the
same-signed gap from borderline (p ≈ 0.057) to significant at the 5% level
(p ≈ 0.031). The effect is *real but modest* (~5–7 points); significance is not
magnitude.

---

## 3. Latency — true TTFT, streaming, 12 trials (separate probe)

Measured with `ttft_probe.py`, which streams both providers and times the first
content token — the responsiveness a user actually perceives, which the
benchmark's non-streaming column cannot capture. Run in `us-west-2` (this
session's ambient region — a *different* region from §1–2, so treat as its own
experiment; only the ranking should be compared across sections).

| metric | GPT-5.6 Luna (Bedrock Mantle) | GPT-4o-mini (OpenAI public) |
|---|---|---|
| TTFT p50 | **0.446 s** | 0.638 s |
| TTFT p95 | 0.669 s | 0.897 s |
| TTFT mean | 0.457 s | 0.691 s |
| Total p50 | **0.673 s** | 1.306 s |
| Total p95 | 1.122 s | 1.580 s |

Luna is ~30 % quicker to first token and ~2× quicker to full completion, and
tighter at the p95. The total-time ranking agrees with the benchmark's latency
column in §1–2, cross-validating both measurements.

---

## How to read this (and what NOT to conclude)

- **Quality comparison is fair.** Byte-identical conversation, same judge/jury,
  provider/network irrelevant to correctness. Luna's ~5–7 point edge is real but
  modest; **GPT-4o-mini is the more *consistent* model** (lower std in both
  experiments) and is far cheaper — the value pick if its ~0.85–0.88 clears your
  bar.
- **Latency is a PLATFORM comparison, not a model-isolated one.** The two models
  are reached over different network paths (AWS endpoint vs OpenAI's public API
  from this machine). The result — "Bedrock/Luna as I'd call it is faster than
  OpenAI/mini as I'd call it" — is a real, decision-relevant thing to know, but
  is not "Luna the model is intrinsically faster." Part of the edge is AWS
  proximity.
- **`p` is not the probability the models are equal.** It is the probability of
  seeing a gap this large *if they were equal* — small `p` means "unlikely to be
  luck," nothing about effect size.
- **Small sample.** 9–10 repeats, one workload, one machine, one time window. The
  paired t-test assumes ~Normal differences; at this n a Wilcoxon signed-rank
  test (symmetry only) is a more robust cross-check. `truncated`/`unjudged` were
  0 across all runs, so no token-ceiling or judge-omission artifacts polluted the
  data.

**Bottom line for this workload:** Bedrock/Luna is faster (clearly, both TTFT and
total) and modestly higher-quality (probably — significant under the jury);
OpenAI/GPT-4o-mini is more consistent run-to-run and cheaper. Pick on cost vs the
quality/latency edge, not on a single headline number.
