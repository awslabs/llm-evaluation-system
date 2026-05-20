# Roadmap

This is the public roadmap for the Agentic AI-Guided Evaluation Platform. Knowing what's coming helps users and contributors plan, and lets the community give direct feedback on direction.

## Categories

| Category | Description |
|----------|-------------|
| **Shipped** | Generally available today. |
| **In progress** | Actively being worked on. Implementation details may still shift. |
| **Coming soon** | Next up. Think a couple of months out, give or take. |
| **Researching** | Being evaluated. The best phase to share use cases or design ideas. |

> The roadmap can change at any time, and items here do not guarantee a feature ships as proposed. We don't publish target dates — operational stability and quality come first.

---

## MCP & Providers

The evaluation engine (Inspect AI) already routes to OpenAI, Anthropic, Google, and Bedrock when the matching API key is present. The remaining provider lock-in is in dataset / judge / report **synthesis**, and in agent **capture**.

### Shipped
- Multi-provider eval execution via Inspect AI (Bedrock, OpenAI, Anthropic, Google)
- Cross-IDE install: Claude Code, Cursor, Kiro, Codex, VS Code (`uvx` flow)
- S3 team sharing with auto-replication and debounced pull-on-read
- File-based per-user storage at `~/.eval-mcp/users/{user}/`

### In progress
- **Provider-agnostic synthesis** — decouple `generate_qa_pairs`, `generate_judge`, and `generate_report` from `BedrockClient` so users without AWS can bootstrap end-to-end on Anthropic or OpenAI keys alone
- **OpenAI / Anthropic SDK capture** — extend `bedrock_capture.py` with bridges for `openai-python` and the Anthropic SDK so agents calling those providers get captured into Inspect logs without code changes

### Coming soon
- Cross-OS / cross-IDE install canary in CI — automated verification on macOS, Linux, and Windows with each release
- Concurrency hardening — load-test boto3 adaptive throttling against Bedrock and the HTTP transport beyond the current ~43 lines of coverage

### Researching
- Multi-tenant eval queue for shared deployments

---

## Frontend

Today the frontend ships a working streaming chat with markdown and tool-call display, plus a results dashboard built on colored text grids. There is no charting library installed and no dedicated pages for Datasets or Judges.

### Shipped
- SSE streaming chat with markdown, tool call → result trace, and file uploads
- Chat history viewer with split-pane session replay
- Results dashboard with per-criterion / per-model score grid and pipeline stage overview

### In progress
- **Data view with real charts** — bar / line / scatter / heatmap for score trends, latency-vs-accuracy, criterion × model matrices, and cost projections
- **Past evals, made browsable** — search, filter, sort, and tagging on the results list; replace 10s polling with push updates
- **Datasets and Judges pages** — first-class browsing UI for both, mirroring what the MCP exposes (today they're agent-only)

### Coming soon
- Side-by-side eval comparison view (pick two past runs, diff scores per sample)
- Code-block syntax highlighting, copy buttons, and message regenerate in chat
- Inline document attachment previews

### Researching
- Eval scheduling and saved templates from the UI
- Shareable per-eval public links (read-only) backed by S3

---

## Eval Engine

Inspect AI is invoked by subprocess with `--adaptive-connections=true`. There's no judge-level cache, throughput hasn't been benchmarked, and the canary pre-flight has not been profiled.

### Shipped
- Inspect AI integration with adaptive connection tuning
- Jury system: multiple judges across model families, binary per-criterion scoring
- PDF report generation with narrative analysis and cost projection
- `retry_evaluation` for resuming incomplete samples

### In progress
- **Judge / scorer caching** — avoid re-scoring identical (response, criterion) pairs across reruns and comparisons
- **Throughput benchmarks** — establish baselines for samples/minute across providers; profile canary and subprocess startup overhead

### Coming soon
- Multi-judge integration tests at the eval-engine level (currently we test capture and subprocess wiring, not full jury runs)
- Cost-aware sample sizing recommendations from the agent

### Researching
- Distributed eval workers backed by SQS / Step Functions for very large runs

---

## Agentic & RAG

Two agentic frameworks are tested end-to-end (Strands, LangChain+LangGraph). RAG today is a single citation criterion in the legal example — there are no first-class retrieval, faithfulness, or grounding scorers.

### Shipped
- Strands and LangChain / LangGraph agent eval via OTLP capture
- Auto-detection of agent entry points (`run_agent`) and OTel bootstrap
- Document-grounded QA generation from PDFs and knowledge bases

### In progress
- **First-class RAG scorers** — deterministic retrieval correctness (right docs fetched), faithfulness (answer grounded in context), hallucination flags
- **Framework-agnostic capture, verified end-to-end** — once the OpenAI and Anthropic SDK bridges land (see *MCP & Providers*), confirm agents built with CrewAI, AutoGen, OpenAI Agents SDK, and the native Claude SDK work without code changes. The goal is the agnostic claim holding, not per-framework code paths
- **30-second quickstart eval** — one bundled example under `examples/` so a new user has something runnable immediately after install

### Coming soon
- Framework-aware pipeline stages (RAG agents → retrieval + context_usage; tool-calling agents → tool_selection)
- Public-benchmark import recipes (MMLU, GSM8K, SQuAD-style) for cross-model comparison

### Researching
- Multi-turn conversation evaluation with checkpoint scoring
- Tool-call sequence scoring (was the right tool picked, in the right order, with the right args?)

---

## Quality & Scale

### Shipped
- Unit and adapter coverage for OTel receiver, agent detection, Bedrock capture, model discovery
- LangChain end-to-end integration test
- Subprocess isolation per agent invocation via ephemeral `uv` venvs

### In progress
- End-to-end eval-engine integration tests across multiple frameworks and judges
- Real failure-mode regression tests (today `test_run_eval_fail_loud.py` only tests the predicate)

### Coming soon
- Concurrent eval scaling tests against Bedrock adaptive throttling
- HTTP transport coverage parity with stdio

### Researching
- Public benchmark harness so contributors can compare scoring stability across releases

---

## Contributing

Found a bug, want a feature, or have a use case the roadmap doesn't cover? [Open an issue](../../issues/new). Community-submitted issues get tagged `proposed` and reviewed by the team. See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup.

## References
- [Inspect AI](https://github.com/UKGovernmentBEIS/inspect_ai) — evaluation framework
- [Inspect AI documentation](https://inspect.aisi.org.uk/)
