# Bundled benchmarks

Benchmarks that ship with this package, discovered from disk. They appear in
`list_benchmarks` alongside the ~129 `inspect_evals` entries (flagged
`bundled: true`) and run through the same `run_benchmark` tool — a caller never
needs to know which catalog a task came from.

```
list_benchmarks                     → both catalogs, one list
get_benchmark_details(<id-or-task>) → full eval.yaml for a bundled one
list_bundled_benchmarks             → all bundled, full metadata, no paging
run_benchmark(task=…, providers=[…]) → runs either kind
```

Bundled benchmarks need no HuggingFace download, no optional dependency group
and no sandbox, so they're the cheapest thing here to run.

## Adding one

Create a directory. **Edit no Python outside it** — not `registry.py`, not the
MCP tools, not `server.py`. The catalog is whatever is on disk.

```
eval_mcp/benchmarks/<name>/
    eval.yaml     metadata — title, tasks, metrics, judge, source, caveats
    task.py       Inspect @task functions, one per entry in tasks[]
    data/         vendored datasets + prompts, verbatim from upstream
    NOTICE.md     provenance: upstream URL, license, pinned 40-char SHA
```

`eval.yaml` mirrors the schema `inspect_evals` uses for its own benchmarks, so
this stays familiar and a port could later be submitted to their register with
little change. Copy `aiwf/eval.yaml` as a starting point. The fields that matter:

| Field | Why |
|---|---|
| `tasks[].name` | Must match an `@task` function in `task.py`. This is the user-facing handle. |
| `tasks[].approx_input_tokens_per_model` | Surfaced before a run so nobody is surprised by the bill. |
| `metrics[]` | First entry is the headline metric reported back to the caller. |
| `judge.default` | Required if judge-scored — the judge is part of the measurement. |
| `source.repository_commit` | Full 40-char SHA. Pinned so fidelity stays re-checkable. |
| `caveats[]` | Anything that makes a score non-comparable (ported subset, different judge, …). |

`task.py` must `import eval_mcp.inspect_patches` so Bedrock applies each model's
own token ceiling instead of Inspect's constant 2048 — see
[CLAUDE.md](../../CLAUDE.md#dont-pass-max_tokens-to-evals--and-dont-reintroduce-a-lookup).

Then, before you trust a single number:

- **Read [the porting contract](../../.claude/skills/port-a-benchmark/SKILL.md).**
  Prompts, rubrics, golden answers, tool schemas and thresholds are **data** —
  copy them byte-for-byte. Only the plumbing gets adapted.
- `tests/test_benchmark_port_fidelity.py` picks up the new directory
  automatically and enforces the structural half of that contract.
- Add a per-benchmark test pinning the actual prompt text (see
  `tests/test_aiwf_benchmark.py` for the diff-against-upstream pattern).

## What's here

| Benchmark | Tasks | Measures |
|---|---|---|
| `aiwf` | `aiwf_medium_context`, `aiwf_long_context` | 30-turn conversation: tool use, instruction following, KB grounding as context grows. Text-mode port of [kwindla/aiewf-eval](https://github.com/kwindla/aiewf-eval). |
