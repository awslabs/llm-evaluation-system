---
name: port-a-benchmark
description: Port an existing third-party benchmark or eval into this repo as an Inspect AI task, faithfully. Use this whenever the task is "add benchmark X", "port this eval", "can we run <github repo> here", or adapting any external eval harness (aiewf-eval, lm-evaluation-harness, a paper's repo, a colleague's script). Enforces the one rule that makes a port worth anything — copy the measuring instrument verbatim, adapt only the runner — plus the verification steps that prove the port is faithful rather than merely running.
---

# Port a Benchmark

A ported benchmark has exactly one job: **produce the same measurement the
original produces.** A port that runs cleanly but measures something subtly
different is worse than no port, because the numbers look authoritative and
nobody can tell they're wrong.

The failure mode is specific and quiet: the dataset gets copied faithfully
(obviously data), while a judge rubric living in a Python string literal gets
"adapted" (looks like code). The suite stays green, because the suite exercises
the plumbing. Only a line-by-line comparison against upstream catches it.

## The rule

> **Copy the measuring instrument verbatim. Adapt only the plumbing.**

Sort every file in the upstream repo into one of two buckets before writing any
code. When unsure which bucket something belongs in, it goes in COPY.

### COPY — byte-for-byte, no edits, no reformatting

Anything that influences what a score means:

- Datasets, questions, golden/reference answers, expected outputs
- **Prompts of every kind** — system prompts, judge/grader rubrics, user-turn
  scaffolding, few-shot examples, instruction preambles
- Tool/function schemas (names, descriptions, required-ness all change model
  behaviour)
- Scoring thresholds, weights, metric formulas, aggregation rules
- Stop conditions, retry/recovery policy, turn boundaries

**Prompts are data, not code.** This is the trap. A prompt in a `.py` string
literal *looks* like code, so it feels adaptable. It isn't. The wording is the
instrument. Rewriting it for concision or clarity is not a refactor — it's
recalibrating a scale nobody asked you to recalibrate.

### ADAPT — rewrite freely

The mechanics of execution, which can't change what's measured:

- Their runner → an Inspect `@task` with a solver + scorer
- Their HTTP/SDK client → `get_model()` / `execute_tools()`
- Their storage/logging → our log dir, viewer, S3 sync
- Their CLI → an MCP tool
- Their concurrency, retries-on-transport-error, progress output

## Workflow

### 1. Read the whole upstream harness before writing anything

Not just the entry point. Specifically read, end to end:

- the runner/pipeline (what constitutes one "turn" or one sample?)
- the scorer/judge (what exactly does it receive? what does it *skip*?)
- the recorder (what gets written, and does the scorer read all of it?)

Score-relevant behaviours hide here that no README mentions. Two real examples
from aiewf-eval, both found only by reading the source: the pipeline sets
`default_tool_result_run_llm = False`, so a turn *ends* at the tool call (no
second generate after the tool result — otherwise the model gets a free retry);
and the judge silently skips recovery records
(`if rec.get("recovery_turn"): continue`), so a tool call made only after a
retry prompt earns no credit. Miss either and every score is inflated.

### 2. Vendor the COPY bucket verbatim, as data files

Put prompts in `data/*.txt`, datasets in `data/*.json` — even when upstream
keeps them in Python. This makes fidelity checkable by `diff` and stops a future
edit from quietly rewording the instrument.

Add a test asserting the vendored copy is unmodified (length + a distinctive
substring is enough).

### 3. When you must edit a copied artifact, transform it in code

Sometimes a genuine edit is required — e.g. dropping a dimension that needs
audio we don't have. Do **not** hand-edit the vendored copy. Load the original
and transform it at import time:

```python
_UPSTREAM = (Path(__file__).parent / "data" / "upstream_judge_prompt.txt").read_text()
_OURS = _strip_audio_dimension(_UPSTREAM)   # mechanical, auditable
```

Then pin it with a test that diffs `_OURS` against `_UPSTREAM` and **fails on any
difference you didn't explicitly justify**:

```python
for line in removed:
    assert is_expected_removal(line), f"unexpected removal: {line!r}"
```

This is the load-bearing guardrail. It converts "someone tidied the prompt" from
an invisible measurement change into a red test.

### 4. Write the ADAPT bucket

Standard Inspect work: solver, scorer, metrics. Route launches through
`_INSPECT_CMD` so `inspect_patches` applies (see CLAUDE.md on `max_tokens`).

### 5. Prove fidelity, not just execution

Green tests do not mean the port is faithful. Run all of these:

- [ ] **Byte-compare the target-facing prompt** against the upstream assembly.
      Reconstruct it from their source and `assert ours == theirs`.
- [ ] **Compare a real score against the upstream published figure**, same model
      if it's in their table. Same ballpark isn't automatic — a large gap means
      a behaviour wasn't ported, so go find it. Don't rationalise it.
- [ ] **Hand-adjudicate the disagreements.** Read the failing samples against
      the golden answers and the source data. Confirm each failure is a real
      model error and not a harness artifact. This is how the recovery-nudge bug
      surfaced.
- [ ] **Real model, never mockllm** (see `never-mockllm-use-bedrock` in CLAUDE.md).

### 6. Write a NOTICE.md next to the port

Upstream repo, license, **pinned commit SHA**, and three lists: what was copied,
what was adapted and why, what was deliberately not ported. Every intentional
deviation gets a line with its justification and, where relevant, its measured
score impact. `eval_mcp/benchmarks/aiwf/NOTICE.md` is the template.

## Judge/grader models

If the port is judge-scored, the judge is part of the measuring instrument:

- Prefer upstream's judge model when it's available to us.
- If you substitute one, **measure the substitution** — fix a transcript, run
  each candidate N times, score against hand-adjudicated verdicts. Never compare
  two judges across two different eval *runs*: each run produces a different
  target transcript, so the delta measures the target model's variance rather
  than the judge's — and can easily point the wrong way.
- Check stability, not just accuracy. A judge that swings 0.100 in the headline
  metric on identical input is unusable regardless of how smart it is.
- Record the judge id in the score metadata so every number is self-describing.

## Anti-patterns

| Don't | Why |
|---|---|
| Rewrite a prompt "more cleanly" | The wording is the instrument. You'd be trading fidelity for concision nobody asked for. |
| Hand-edit a vendored copy to make one small change | Untraceable. Transform the original in code and pin with a diff test. |
| Let one legitimate edit license a general tidy-up | Dropping an unusable dimension is valid; restructuring the surrounding 100 lines is not. |
| Assume a score gap is "just a different judge" | It's usually an unported behaviour. Go find it before you attribute it. |
| Treat a passing test suite as proof of fidelity | The suite exercises the plumbing, not whether the instrument reads true. |
| Compare judges across separate runs | Different transcripts, so you measure the target's variance, not the judge's. |

## Checklist

```
[ ] Read upstream's runner, scorer AND recorder end to end
[ ] Sorted every artifact into COPY vs ADAPT (unsure -> COPY)
[ ] Prompts/datasets vendored verbatim as data files
[ ] Any edit to a copied artifact done as a code transform, pinned by a diff test
[ ] Test asserts the vendored copy is unmodified
[ ] Target-facing prompt byte-compared against upstream's assembly
[ ] Real Bedrock model run (never mockllm)
[ ] Score compared against upstream's published figure; gaps explained, not excused
[ ] Failing samples hand-adjudicated against golden answers
[ ] NOTICE.md: upstream, license, pinned SHA, copied/adapted/not-ported, deviations + impact
[ ] Judge model justified by measurement if it differs from upstream's
```
