# Model comparison examples

Scripts for comparing two target models on the bundled **aiwf** multi-turn
benchmark, with repeats and a paired significance test — plus a streaming TTFT
latency probe. Thin drivers over the shipped `run_multiturn_benchmark` handler;
they add repetition and arithmetic, not new measurement.

| File | What it does |
|---|---|
| `compare_models.py` | Runs two models through the same conversation N times (single judge or `--jury`), reports per-model pass_rate mean/std, per-dimension counters, latency, and a paired t-test. |
| `ttft_probe.py` | Streams both providers and times the first token — true TTFT, which the benchmark (non-streaming) can't measure. |
| `RESULTS.md` | A worked run: GPT-5.6 Luna (Bedrock) vs GPT-4o-mini (OpenAI), single-judge + jury + TTFT, with how to read it and what not to conclude. |

## Auth

- **Bedrock / Mantle** models (`bedrock/*`, `openai/bedrock/*`): ambient AWS
  credentials, and `AWS_REGION` (defaults to `us-east-2`).
- **OpenAI** models (`openai/gpt-*`): `OPENAI_API_KEY` in the environment. These
  scripts read only the env var — nothing is written to disk — so a temporary
  key can be used and revoked afterward.

## Caveats worth reading before trusting numbers

- Quality comparisons are fair (identical conversation, fixed judge). Latency is
  a **platform** comparison (different network paths), not model-isolated.
- Small samples: a paired t-test assumes ~Normal differences; at ~10 repeats
  treat `p` as a guide. `RESULTS.md` spells out the full set of caveats.
