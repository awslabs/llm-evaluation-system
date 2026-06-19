# Agent Evaluation Architecture — Design Guidance

> Working design for evaluating, experimenting with, and optimizing **arbitrary agentic
> systems** (multi-step, tool-using agents — any framework, any model provider).
> This is the *target* architecture and the reasoning behind it, not a description of
> what the codebase does today. Where it differs from the current implementation, that's
> called out under [Where this leaves the current system](#where-this-leaves-the-current-system).

Framing borrows from Han Lee, *"Hidden Technical Debt of AI Systems: Agent Evaluation
Infrastructure"* (June 2026), which extends Sculley et al. 2015. The two ideas we lean on:
the **control-plane / data-plane** split, and **"agents need worlds, not datasets."**

---

## 0. TL;DR — the decisions

| Question | Decision |
|---|---|
| Eval engine core | **Inspect AI** — it *re-executes* agents (the prerequisite for experiments) |
| Trace capture | **OpenTelemetry GenAI semantic conventions** (`gen_ai.*`) — vendor-neutral |
| Instrumentation libs | **Official OTel `opentelemetry-instrumentation-genai-*` + botocore**; third-party (OpenLLMetry/OpenInference) only to fill a framework gap (e.g. LangGraph/CrewAI), normalized into `gen_ai.*` |
| Observability platform (Langfuse/Phoenix/Braintrust) | **Not required.** They're storage+UI, a separate concern. Adopt only to outsource the viewer; stays swappable because everything rides OTLP. |
| Optimization | Keep the closed-loop optimizer; feed it **full trajectory** signal (GEPA-style), not just a scalar |
| What references to score against | **Outcome/goal first, code never.** Reference-free wherever possible. |

The one structural change from where we are: **make the scoring engine read OTel `gen_ai.*`
spans from any agent**, instead of capturing only AWS Bedrock calls.

---

## 1. Two modes — observation vs experiment

The single most clarifying distinction. They are *complementary jobs*, not competing tools.

| | **Observation** (Mode 1) | **Experiment** (Mode 2) |
|---|---|---|
| Physics analogy | astronomy / field data | the lab |
| What happens | score traces of runs that **already happened** | **re-run** the agent under controlled conditions |
| Agent executes? | No | Yes (this is what a *harness* does) |
| Answers | "how is the deployed agent behaving?" | "is version B better than A?" |
| Tool | a trace store (Langfuse/Phoenix) suffices | a re-execution harness (**Inspect**) is required |

**Why traces alone are not enough.** A trace is an observation. It cannot tell you what
*would* have happened with a different prompt/model/tool — there is no trace of a version
you haven't run. Evaluation-as-a-decision ("which do I ship?") and optimization are
**intrinsically experimental**: vary one thing, hold the rest fixed, re-run, compare.

**Why observation still matters.** Production traces (a) monitor live behavior and (b) are
the richest *source* of real inputs and starting states to seed experiments. Observation
feeds the experiment's task distribution — the production→eval loop Lee argues for.

**The bridge between them:** a trace's captured state can become the *initial condition* of
a controlled re-run ("restart the system from a trace"). That turns an observation into a
repeatable experiment. It's the highest-value, least-built capability in the space.

---

## 2. The stack — three layers, and only one is forced to differ

```
        EXECUTION  (must differ between modes)        TRACE CONTRACT       SHARED DOWNSTREAM
  ┌───────────────────────────────────────┐
  │ Experiment:  Inspect runs the agent    │──┐
  │   in a controlled dev environment      │  │
  └───────────────────────────────────────┘  │     ┌──────────────┐    ┌──────────────────────┐
                                              ├───► │ OTel GenAI   │──► │ projection → scorers │
  ┌───────────────────────────────────────┐  │     │ gen_ai.*     │    │ jury / deterministic │
  │ Observation: prod agents instrumented  │──┘     │ (ONE schema) │    │ trajectory / stages  │
  │   via OTel (or Langfuse-OTLP / Phoenix)│        └──────────────┘    │ + optimizer / A·B    │
  └───────────────────────────────────────┘             ▲              └──────────────────────┘
                                            replay/branch: a trace's captured state →
                                            initial condition of a controlled experiment
```

- **Execution layer differs** (you can't re-run production; you can't observe a version you
  haven't run). This is irreducible — it *is* the observation/experiment split.
- **Trace + scoring layers are shared** *if and only if* both execution modes emit the same
  trace schema. That shared schema is the whole game.

**OTel is the membrane.** Above it, execution differs. Below it, everything — projection,
scoring, optimization, viewer — is written once and is blind to where the trace came from.
This is why "the same eval runs on production data and experiments" is achievable: not one
tool doing both, but one *contract* both feed.

---

## 3. Capture — getting all the data

### Mechanism
OTel instrumentation is **in-process monkeypatching**: it wraps the SDK/framework call
(`messages.create`, `chat.completions.create`, botocore `converse`, the framework's
tool-runner) and records request kwargs + response into a span. Not a proxy, not network
sniffing — it intercepts the Python call.

### What gets captured, and the dividing line
Everything that **crosses an instrumented boundary**:
- ✅ Every LLM call: full input messages, system prompt, tool definitions, output, finish reason
- ✅ Token usage (incl. cache/reasoning sub-counts), model id, **sampling params (temp, top_p, top_k, seed, penalties)**
- ✅ The model's **request to call a tool** (name + args) — it's in the response object
- ✅ Tool **execution + result** — *only if an instrumented framework runs the tool*
- ✅ Agent / sub-agent / workflow / plan spans, latency, errors, correct parent/child nesting

**The load-bearing rule:** capture records *function arguments and return values at
instrumented boundaries — nothing more.*
- Model *decides* to call a tool → always captured (it's in the LLM response).
- Tool *executes* → captured **only if** the executor is wrapped. Framework tool-runner
  (LangChain, OpenAI Agents) → clean `execute_tool` span. A hand-rolled loop → no execution
  span (you only see the result fed back into the next LLM call).
- *Practical robustness:* prefer instrumented frameworks, and make tool functions return
  enough in their result to be meaningful.

### Proxy vs in-process
A proxy (Inspect's `sandbox_agent_bridge`, LiteLLM, a `base_url` shim) is framework-agnostic
and zero-code, but **structurally blind to anything not crossing the model API**: tool
execution, internal reasoning, memory. In-process instrumentation sees those (with the right
instrumentor installed). Use in-process as primary; proxy only as a fallback for
un-instrumentable agents, knowing it's lossy on trajectory.

### What is uncapturable by ANY method (build for it explicitly)
- **Environment state-deltas** — a tool span records args + return value, *not* what rows
  changed / files were written. The mutation happens *below* the instrumented boundary.
  → Capture by **snapshotting the environment before/after the run** and diffing.
- **Memory reads/writes** — invisible unless the memory layer is itself instrumented as tools.

### Tooling choice
- **Schema:** OTel GenAI conventions (`gen_ai.*`). Vendor-neutral, no Elastic license, no
  Arize/Traceloop governance. Status is "Development" (pre-GA) — pin versions, keep the
  receiver tolerant of attribute drift, don't hard-code a frozen schema.
- **Instrumentation:** official OTel `opentelemetry-instrumentation-genai-*` (OpenAI,
  Anthropic, Google, LangChain, agent SDKs) + `opentelemetry-instrumentation-botocore` for
  Bedrock. These cover the providers/frameworks that matter today.
- **Gaps:** no official LangGraph / CrewAI instrumentation. *Only there* reach for a
  third-party instrumentor (OpenLLMetry/OpenInference) — and **normalize its spans into
  `gen_ai.*`**, never adopt its schema as your source of truth.
- Package-name trap: PyPI `opentelemetry-instrumentation-langchain` is **Traceloop's**, not
  official. The official one publishes as `opentelemetry-instrumentation-genai-langchain`.

> **Why not Langfuse/Phoenix/Braintrust for capture?** They capture by the *same*
> monkeypatch mechanism but emit *their own* schema and exist to sell *storage + UI*. For
> capture they add nothing over OTel and only impose a proprietary schema. Adopt one only if
> you want to stop maintaining your own receiver/viewer — and even then via its **OTLP
> endpoint** so you stay unlocked.

---

## 4. Running the eval — what a scorer is

Once you have a trace (from either mode), an eval is just **functions over the projected
trace**. Project the OTel span tree into a flat record once:

```
{ output, [llm_calls], [tool_calls(name,args,result)], trajectory, usage, [state_delta] }
```

Scorers consume the **projected record, never raw spans** — so capture, source (prod vs
experiment), and scoring stay decoupled and swappable.

Three scorer kinds, used together:

| Kind | Example | Notes |
|---|---|---|
| **Deterministic** | `expected_tools ⊆ called`; schema/regex on args; **diff over state-delta** | free, exact, no judge variance — *prefer wherever the answer is checkable* |
| **LLM judge / jury** | answer quality vs criteria; "was this path sensible?" | multi-judge, multi-family, majority vote — for genuinely subjective questions |
| **Trajectory / stage** | empty-tool-hallucination; per-stage pass/fail | localizes *where* it failed |

**Context filter** is a first-class knob: show the judge only the slice that matters for the
question (final output / tool-calls-only / first-response / full trajectory).

---

## 5. Experiment methodology — finding *what to fix*

Two disciplines turn "I have a score" into "I know what to fix":

1. **Score per stage, not just end-to-end.** Decompose the trajectory
   (`routing → tool selection → arg quality → execution → final answer`) and score each.
   A failed run then tells you *where*: "passed routing, right tool, **wrong args**."
2. **Vary one thing, hold the rest fixed.** Change exactly one variable and re-run the same
   dataset to isolate cause. Variables include the **code/architecture itself** (see §7).

The loop:
```
1. Run dataset through agent (dev env) → OTel trace (+ state snapshot) per sample
2. Score each stage: deterministic where checkable, judge/jury where subjective
3. Aggregate failures BY STAGE → localize the weak component
4. Hypothesis ("tool args are wrong")
5. Experiment: vary ONLY that component, hold rest fixed, re-run
6. Compare: did that stage improve WITHOUT regressing others?
7. Keep the winner; repeat (manual, or via the optimizer)
```

Aggregating failures by stage produces directly actionable output:
```
tool_selection: 94% ✓   tool_arguments: 61% ← THE PROBLEM   final_answer: 88% ✓
```

This feeds the **optimizer**: the richer the per-stage / full-trajectory failure signal, the
more targeted the proposed fix (GEPA-style trace-driven optimization beats scalar-reward RL
for prompt-level work, and OTel traces are exactly that rich signal).

---

## 6. The dev environment

- Experiments run against a **controlled dev/test environment** (test DB, fixtures, scratch
  fs) — **never production.** Running experiments against prod confounds results and causes
  real side-effects.
- **Sandbox (Docker) is optional**, not mandatory. The agent *always* really runs; the
  sandbox only isolates/controls the *world* it runs in. Use it when the agent writes files /
  runs shell / needs a pristine per-sample world (and for clean state-delta capture).
  For "calls a model + a dev API," a controlled dev environment without a heavyweight sandbox
  is fine.
- **Reset between samples is the real requirement.** If run A's writes leak into run B, the
  experiment is confounded. Re-seed the DB / clear the scratch dir between samples. The
  sandbox is just one (heavier) way to get this reset for free.

---

## 7. The code is a *variable*, not the standard

**Do not derive correctness from the agent's code.** If you do, a stupid implementation gets
certified as "correct" because the agent did what the (dumb) code made it do. The eval
becomes a mirror, not a judge.

- **Anchor correctness to the goal/outcome**, which comes from *outside* the code (§8).
- Use **code analysis only to discover the *surface*** — what tools/stages/components exist,
  i.e. *what you can vary*. (This is the legitimate use of static analysis: a map of the
  search space, not a definition of correct behavior.)
- Then treat every component as a **knob**: add / remove / swap / reorder, run against the
  fixed outcome eval, keep what wins. This is **ablation applied to the architecture** — it
  can *prove* a stage is dead weight ("delete stages 3–4, no outcome loss, 40% cheaper").

```
   GOAL / OUTCOME  ──► FIXED reference (anchored outside the code; doesn't move)
          ▲ scored against
   AGENT CODE  ◄── the VARIABLE you mutate (stages, tools, sub-agents, prompts)
          │ each variant → run → trace → score vs fixed outcome → compare
```

Rule: **the reference must come from the goal, never from the code being tested.**

---

## 8. What to evaluate against — references

### You are NOT limited to reference-based evals
Reference-based ("match a golden answer") is one family, and for agents often the *weakest
and least necessary*. Treat references as a **last resort**, used only for absolute factual
correctness on **non-acting** tasks.

### Reference-free options, strongest first
1. **Environment-grounded** (no reference, deterministic) — the task encodes its own success:
   "is the flight booked?", "do the tests pass?". The **state-delta is the ground truth**.
   *Lean here hardest for any agent that acts.*
2. **Invariant / property checks** (reference-free) — must-always-hold properties: no
   hallucinated tool result, output grounded in retrieved context, stayed in permission
   bounds, valid format, under cost/latency budget, no infinite loop.
3. **Pairwise comparison** (reference-free) — "input X: is v1 or v2 better?" *More reliable
   than absolute scoring* and answers the optimization question ("did my change help?")
   directly, with no fixed standard.

### Decision rule
```
Does the agent change world state?           YES → environment check.   no reference ★
  else  Is the question about VALIDITY?       YES → invariant checks.    no reference
  else  Comparing two versions?               YES → pairwise judge.      no reference ★
  else  Need absolute factual correctness?    YES → reference required → build one (§8.1)
```

### 8.1 When you DO need a reference — how to create it
By cost/quality:
1. **Synthetic from source docs** — derive Q + golden answer from a ground-truth document
   (cheap, scalable, sanitized distribution; good cold-start / coverage).
2. **Label a production-input subset once** (best for realism) — mined real inputs + a
   human/strong-model-written answer. Real distribution + an answer key, amortized forever.
3. **Weak supervision** — strong model drafts, human spot-checks (~10× cheaper).
4. **Execution-derived** — for anything runnable (code, SQL, math, API), the *execution
   result* is the reference. (Really §8 item 1 wearing a reference hat.)

---

## 9. Who rates — humans, models, simulated users

You are **not** limited to human ratings. At scale, models do the bulk; humans calibrate and
override.

| Rater | Cost | Role |
|---|---|---|
| Deterministic / environment | ~free | truth wherever checkable |
| **LLM jury** (multi-family, majority vote) | cheap | the workhorse; scales infinitely |
| **Simulated user** | cheap | the *actor* that drives the agent (multi-turn, natural variability) |
| **Human** | expensive | calibration sample + override + context — *not* a throughput layer |

### Keep the actor and the judge separate
- **Simulated user = actor** (plays the user, drives the conversation; tau-bench pattern).
- **Judge = rater** (a *different* model, scores the result).
Never let one model both drive and grade — it grades its own conversation favorably.

### Two traps
1. **The judge must score against the GOAL, not the code/trace.** The trace is *evidence the
   judge looks at*; the *standard* is the outcome/intent. Otherwise the judge rubber-stamps
   the implementation (the §7 problem, re-entering through the judge).
2. **Who validates the judge?** Three models agreeing ≠ truth (shared blind spots,
   self-preference bias). Defenses: **jury across model families** + a **small human-labeled
   sample** measuring judge-vs-human agreement. The human's job is to **validate the rater**,
   not to be the rater.

### Humans: able, not required
Humans can modify any verdict — not because they're correct (humans are noisy and biased
too), but because they carry context the system lacks and serve as the circuit-breaker when
the loop is confidently wrong. **Every human override is logged as another signal that
recalibrates the judges.** If humans keep overriding the judge the same way, the judge is
miscalibrated — fix it.

---

## 10. Signals are partial — triangulate, none is sovereign

The honest stance: every signal is fallible in a different way. Combine by **role**, and
treat **disagreement as information**.

| Signal | Strength | Weakness | Job |
|---|---|---|---|
| Goal / outcome | reliable (anchored to truth) | **sparse** — one bit, no localization | the **verdict** |
| Trace | dense, full path | just *what happened*, not *what should* — and **mutable** | the **diagnosis** |
| Jury | scalable quality judgment | model bias / variance | quality where subjective |
| Human | context + veto | inconsistent, biased, rushed | calibration + override |

- **Goal anchors correctness; trace supplies resolution/diagnosis** (and stays a *variable*,
  so it informs without becoming dogma). Using the trace as *evidence* anchored to the goal
  is **not** circular; using it as the *standard of correctness* is.
- Where signals **agree** → confident, cheap. Where they **disagree** → that's the valuable
  case flagged for attention.
- No single source defines truth. A jury beats a judge; humans calibrate rather than dictate
  — not because humans are truth, but because an independent (noisy) signal improves
  triangulation.

---

## 11. Code ↔ trace duality (eval construction)

| | **Code (static)** | **Traces (dynamic)** |
|---|---|---|
| Answers | what the agent is *supposed* to do | what it *actually* did |
| Gives the eval | its **structure** (which scorers, stages, expected tools, invariants, criteria) | its **data** (real inputs, starting state, failure cases, candidate references) |
| Reliable for | the rubric skeleton + the search space to vary | the test cases + a reality-check on the rubric |

- **Code → structure.** Static analysis derives stages, expected-tool maps, invariants,
  judge criteria — and the surface of components to vary (§7). *Not* the definition of
  correct behavior.
- **Traces → data.** Real inputs become the dataset; failing trajectories become regression
  tests; a *confirmed-good* trace's tool calls become the expected path **for free** (you
  derive trajectory expectations from a single confirmed outcome).
- **Traces correct the code-derived rubric.** "Code says tool X for weather; prod shows X 80%
  / Y 20%" — that discrepancy is itself a finding. **Code proposes, traces dispose.**
- **Sequencing:** cold-start from code-analysis + synthetic data (eval from day one); as
  traces accumulate, swap in mined real inputs, add failure regressions, and recalibrate.

---

## 12. The full picture

```
  INPUTS:  prod traces (real)  ∪  simulated-user (generated)  ∪  synthetic (cold-start)
                                   │
                                   ▼
              Inspect runs agent in CONTROLLED DEV ENV  →  OTel trace + state-delta
              (reset between samples; sandbox if it acts on the world)
                                   │
                ┌──────────────────┼───────────────────┐
                ▼                  ▼                    ▼
        deterministic /        LLM JURY            simulated-user
        environment            (multi-family,        satisfaction
        checks (truth)         scored vs GOAL)
                └──────────────────┼───────────────────┘
                                   ▼
                projection → per-stage scores → failure localization
                                   │
                ┌──────────────────┼───────────────────┐
                ▼                                       ▼
        HUMAN CALIBRATION                    optimizer / A·B
        (rate a SAMPLE → judge-vs-human;     (vary ONE thing — incl. the
         override anything → recalibrates)    architecture — compare vs GOAL)
```

**Principles that keep it sane:**
1. Prefer deterministic / environment truth over judges wherever possible.
2. Judges score against the **goal, never the code**.
3. Humans **validate** the judge on a sample; they don't replace it (and may override anything).
4. The **code is a variable** you test, never the standard you test against.
5. Scorers consume the **projected trace**, so capture / source / scoring stay decoupled.
6. **OTel `gen_ai.*` is the one contract** every downstream consumer reads.

---

## Where this leaves the current system

The current `eval_mcp` already has most of the hard pieces:
- **Inspect AI** as the engine ✓ — keep it (it re-executes agents; that's the experiment prerequisite).
- **Jury** (multi-family, binary-per-criterion, majority vote) ✓
- **Pipeline-stage trajectory scoring** with context filters ✓
- **Deterministic tool-call scorers** ✓
- **Closed-loop prompt optimizer** ✓ — extend to consume full-trajectory failure signal.
- **Code → structure** via `analyze_agent_path` ✓ (use it for the *search space*, not as the correctness standard).

The gaps to close, in priority order:
1. **Capture breadth — the one structural change.** Today the OTLP receiver captures **only
   AWS Bedrock** botocore calls and is blind to OpenAI-direct / Anthropic-direct / local
   models. Add official OTel `opentelemetry-instrumentation-genai-*` instrumentors to the
   agent subprocess and **normalize all spans to `gen_ai.*`**, so the engine works on *any*
   agent. Make the projection layer (`eval_results.py` / receiver) read `gen_ai.*`.
2. **Reproducibility stamping.** Stamp config/prompt/judge-criteria/package-version/seed into
   every eval log (OTel GenAI conventions already define the attributes). Kills "cargo-cult
   eval" / measurement-drift debt.
3. **Trace-source unification.** Run the *same* scorers over production traces (Mode 1) and
   experiment traces (Mode 2) by reading the shared `gen_ai.*` projection.
4. **State-delta channel.** Snapshot the dev environment around Mode 2 runs so scorers can
   grade *what the agent did to the world*, not just what it said. (Genuinely unbuilt
   off-the-shelf — the real frontier.)
5. **Human-in-the-loop rating + calibration.** Rate-not-author outcomes; measure
   judge-vs-human agreement on a sample; log overrides as recalibration signal.

---

## Caveats

- OTel GenAI conventions are **"Development" status** (pre-GA, breaking changes allowed). Pin
  versions; keep the receiver schema-tolerant. It's the right *standards* bet, but it's still
  baking — third-party instrumentors are more mature *today* at the cost of being vendor
  schemas.
- The **environment / memory** surfaces (state-deltas, memory pollution) are unmet by
  off-the-shelf tooling — building the snapshot channel is real work, not integration.
- **No single signal is ground truth** — including humans. The architecture's reliability
  comes from triangulation, not from any one authoritative rater.
