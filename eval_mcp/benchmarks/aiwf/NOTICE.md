# Attribution: aiwf multi-turn benchmark

The benchmark data and evaluation design in this directory are derived from
**[kwindla/aiewf-eval](https://github.com/kwindla/aiewf-eval)** by Kwindla
Hultman Kramer, used under the MIT License.

Vendored from commit `d0dabb6c473d8e8d5a84a5504af3f68ec50e9e70` (2026-06-14).

## What was taken

| Ours | Upstream |
|------|----------|
| `data/turns.json` | `benchmarks/_shared/turns.py` (30 turns; `audio_file` key dropped, `index` added) |
| `data/kb_medium.txt` | `benchmarks/aiwf_medium_context/data/knowledge_base.txt` (verbatim) |
| `data/kb_long.txt` | `benchmarks/aiwf_long_context/data/knowledge_base.txt` (verbatim) |
| `data/system_preamble.txt` | `_PREAMBLE` in `benchmarks/*/prompts/system.py` (verbatim) |
| `data/system_tools_section.txt` | `_TOOLS_SECTION` in the same file (verbatim) |
| tool schemas in `data_loader.py` | `benchmarks/_shared/tools.py`, transcribed from Pipecat `FunctionSchema` to Inspect `ToolDef` |
| judge rubric in `task.py` | `src/multi_turn_eval/judging/claude_judge.py`, adapted (see below) |

The two knowledge-base variants share identical turns, tools and prompt
scaffolding — verified by test, and the reason one copy of each prompt block
serves both.

## What was changed, and why

**Runner.** Upstream runs the conversation through a Pipecat pipeline; this is
an Inspect AI task. The observable behaviours that affect scores were ported
deliberately, not incidentally:

- *Turn boundary.* Upstream's text pipeline sets
  `default_tool_result_run_llm = False`, so a turn ends at the tool call. We do
  the same — the tool result enters the context for the next turn, but there is
  no second generate within the turn.
- *Recovery nudge.* Upstream injects one synthetic `"Please go ahead."` turn
  when a turn expected a tool call and none was made
  (`_should_recover`/`MTE_ENABLE_RECOVERY`, default on), merging that attempt
  into the same scripted turn. Ported. Measured on claude-haiku-4-5 over 18
  turns: pass_rate 0.833 without it, 0.944 with — it is not optional if the
  numbers are meant to resemble upstream's.

**Judge.** Same three dimensions, same one-call-over-the-whole-conversation
shape (which is what makes upstream's early/late tool-call realignment
possible), and the rubric text follows upstream closely. Two differences:

- Model: upstream uses `claude-opus-4-5` via the Claude Agent SDK; we use our
  configured Bedrock judge (`claude-opus-5` by default, overridable per run —
  see the bake-off below).
- Upstream's two-phase output (`phase1_analysis` then `final_judgments`) is
  collapsed to a single forced tool call with per-turn verdicts.

Consequence: **this reproduces the benchmark, not upstream's exact figures.**
Relative model ordering should hold; absolute numbers will differ.

### Choosing the judge: measured against hand-adjudicated verdicts

**How not to do this.** Comparing two judges by running the benchmark twice and
diffing the scores is invalid — each run produces a *different* model transcript,
so the delta measures the target model's variance, not the judge's. We made that
mistake and it pointed the wrong way. The valid method: fix a transcript, run
each candidate judge over it N times, and score them against verdicts adjudicated
by hand from the transcript and knowledge base.

Ground truth used (2 transcripts, 30 turns each, contested turns read manually):

| Turn | Verdict | Why |
|---|---|---|
| T1 12 | FAIL | expected `submit_session_suggestion`; model confirmed the *previous* suggestion, call only came after the nudge |
| T1 18 | FAIL | user asked about SF weather (out of scope); model didn't deflect, it recapped the tech-support submission |
| T1 13 | PASS | answer scoped to the user's stated day; all facts correct. Omission ≠ contradiction |
| T2 9 | FAIL | golden offers to submit a suggestion; model deflected to external contacts |
| T2 27 | PASS | correct time/place, and all 8 meetup topics it added are verifiably in the KB. Extra *correct* detail is allowed |

Results, 16 calls per judge across both transcripts:

| Judge | Errors | False pos | False neg | Exact-match runs |
|---|---:|---:|---:|---|
| **`claude-opus-5`** | **2** | 2 | 0 | 14/16 |
| `claude-opus-4-5` (upstream's) | 5 | 0 | 5 | 11/16 |
| `claude-sonnet-4-6` (previous) | 9 | 8 | 1 | 7/16 |

Opus 5 wins, and the failure modes differ in a way that matters: Sonnet 4.6's
errors are almost all **false positives** — it failed T2 turn 27 on 8 of 8 calls,
penalising an answer that is factually correct and merely more detailed than the
golden text. That's the worst kind of judge error here, because it makes a good
model look bad and the number still looks plausible. Opus 4.5 errs the other way
(misses real failures). Cost is not a factor at this scale: ~$0.13 per
benchmark run vs ~$0.08.

### Rejected: Sonnet 5 as judge — too unstable

Sonnet 5 swings **0.100 in `pass_rate` across repeated calls on identical
input**, flip-flopping on six different turns (9, 11, 12, 14, 16, 17, 24 each
failing 1–2 times out of 5). That's judge noise indistinguishable from a real
difference between two target models. It is also uniformly stricter rather than
more accurate, including the same T1-turn-13 false positive. A benchmark needs a
stable ruler more than a clever one.

### Rejected: upstream's closing "Remember" reminder block

Upstream's judge prompt ends with a recap including *"Be generous with
kb_grounding unless there's a clear factual error"*. Adding it verbatim did
**not** fix the turn-13 false positive, and it traded determinism for leniency
(spread 0.000 → 0.033 on both transcripts). The rubric already states each rule
once in its dimension section; repeating them only loosened grading.

## What was deliberately not ported

Everything speech-to-speech — roughly 5,900 lines upstream, versus ~2,800 for
the text path:

- The four Pipecat pipelines (`realtime`, `nova_sonic`, `grok_realtime`,
  `audio_in`) and the audio transports. Inspect has no duplex-audio transport,
  so supporting speech models would mean embedding upstream's runner rather
  than writing an Inspect task.
- `benchmarks/_shared/audio/` — 31 WAVs, 5.9 MB, the spoken form of each turn.
- The `turn_taking` judge dimension and all timing analysis (Silero VAD, 2 kHz
  audio-tag alignment, voice-to-voice latency). Undefined without audio.

Upstream's non-Bedrock providers (Cerebras, Ultravox, Groq, xAI) also don't come
along; models are whatever Bedrock serves.

## Why it lives here rather than in `inspect_evals`

As of 2026-05-08 `inspect_evals` no longer accepts new eval code (see its
`EVAL_REGISTER.md` — a dependency-isolation decision). New evals are expected to
live in the author's own repo and be *listed* upstream via the register.
Register entries are not shipped in the `inspect_evals` wheel, so nothing in the
register is runnable through our `run_benchmark`. Hence: bundled here, launched
by absolute path via `run_multiturn_benchmark`.
