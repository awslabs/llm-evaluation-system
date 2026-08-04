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
  configured Bedrock judge (Sonnet by default, overridable per run).
- Upstream's two-phase output (`phase1_analysis` then `final_judgments`) is
  collapsed to a single forced tool call with per-turn verdicts.

Consequence: **this reproduces the benchmark, not upstream's exact figures.**
Relative model ordering should hold; absolute numbers will differ.

### Judge choice was measured, not assumed — two negative results

Both were tried and rejected. Don't redo them without new evidence.

**1. Sonnet 5 as judge — rejected, too unstable.** It looks better on a single
run (96.7% vs 93.3% for the same target) but that comparison is invalid: the two
runs had *different* model transcripts. Holding the transcript fixed and varying
only the judge, 5 calls each:

| Transcript | Judge | pass_rate spread over 5 calls | Turns failed (count of 5) |
|---|---|---:|---|
| T1 | sonnet-4-6 | **0.000** | 12×5, 18×5 |
| T1 | sonnet-5 | 0.000 | 12×5, 13×5, 18×5 |
| T2 | sonnet-4-6 | 0.033 | 9×5, 27×2 |
| T2 | sonnet-5 | **0.100** | 9×5, 11×2, 12×1, 14×2, 16×2, 17×1, 24×1 |

On T2 Sonnet 5 flip-flops on six different turns across identical inputs — a
0.100 swing in the headline number from judge noise alone. It is also uniformly
*stricter*, not better in both directions, and one of its extra failures is
wrong: on T1 turn 13 it fails `kb_grounding` because the assistant scoped a
correct answer to the one day the user said they were attending, which is an
omission, not the "clear factual contradiction" the rubric requires.

A benchmark needs a stable ruler more than a clever one. Sonnet 4.6 stays.

**2. Upstream's closing "Remember" reminder block — rejected.** Upstream's judge
prompt ends with a recap including *"Be generous with kb_grounding unless there's
a clear factual error"*. Adding it verbatim did **not** fix the turn-13 false
positive, and it traded determinism for leniency on the default judge (spread
0.000 → 0.033 on both transcripts, mean 0.933 → 0.940/0.953). The rubric already
states each rule once in its dimension section; repeating them only loosened
grading. Not ported.

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
