# Architecture

Three diagrams covering the three distinct ways code in this repo runs:

1. **[Eval execution](#1-eval-execution)** — what actually happens when a user says "evaluate this." End-to-end from MCP tool call through Inspect AI, Bedrock, the jury of judges, and back into the viewer.
2. **[MCP server](#2-mcp-server)** — how the `eval-mcp` package is wired: transports, tool surface, storage, the local viewer, optional S3 team sharing.
3. **[EKS deployment](#3-eks-deployment)** — the optional multi-user web app: CloudFront → ALB → backend pod (with the MCP as a K8s native sidecar) → S3/RDS.

The MCP package (`eval_mcp/`) and the EKS web app (`backend/` + `frontend/` + `helm/` + `infra/`) are independently deployable. The web app embeds the MCP as a sidecar in the backend pod — that's the one place the two intersect.

---

## 1. Eval execution

```mermaid
flowchart TB
    %% Nodes
    IDE["IDE coding agent"]
    Tools["eval-mcp tools"]

    subgraph Inspect["Inspect AI subprocess (per sample)"]
        direction TB
        Fork{"Eval type?"}
        Standard["Standard:<br/>call target model"]
        Agent["Agent:<br/>spawn subprocess,<br/>OTel-instrumented"]
        Output["Model output"]
        Jury["Jury scoring<br/>(N judges, binary per criterion,<br/>majority vote)"]
    end

    Bedrock[("AWS Bedrock<br/>target + judge models")]
    Log[(".eval log<br/>+ raw OTel JSONL")]
    Viewer["Local viewer<br/>+ PDF report"]

    %% Flow
    IDE --> Tools
    Tools -->|"spawn"| Inspect

    Fork -->|"standard"| Standard
    Fork -->|"agent"| Agent
    Standard --> Output
    Agent --> Output
    Output --> Jury

    Inspect -->|"all model calls<br/>(target + judges)"| Bedrock
    Inspect --> Log
    Log --> Viewer
```

**Tool order in a typical session:** `list_bedrock_models` → `generate_qa_pairs` (from docs or context) → `save_dataset` → `generate_judge` → `create_eval_config` → `run_evaluation` → `generate_report`. The agent in the IDE picks the order; the MCP just exposes the tools.

**Why subprocess isolation.** `run_evaluation` shells out to `python -m inspect_ai eval` rather than calling Inspect in-process. A cancelled or crashed eval can't take down the MCP, and the subprocess gets a fresh interpreter so OTel instrumentation can be installed cleanly per run.

**How agent evals capture Bedrock calls.** For `agent_path` configs, the solver spawns the agent as a subprocess with `opentelemetry-instrument` autoloaded (via `opentelemetry-distro`) and `OTEL_EXPORTER_OTLP_ENDPOINT` pointed at an in-process OTLP receiver inside the Inspect subprocess. The agent's Bedrock calls emit spans → receiver → ModelEvents in the `.eval` log. A pre-flight canary in `eval_mcp/canary.py` exercises this path once before the real eval, so a broken capture pipeline fails loudly instead of returning `success=true, scores=[]`. Raw spans are also appended to `logs/raw_otel/<eval_id>.jsonl` as cold storage in case the projection ever drops data.

**Jury scoring.** Multiple judges from different model families (default in `eval_mcp/core/judge_config.py`) each score every sample binary-per-criterion. `backend/core/jury_scoring.py` aggregates: majority vote per criterion, then sample passes if all criteria pass. This is more reliable than single-judge numeric scales ([Mallinar et al., 2025](https://arxiv.org/abs/2503.23339v2)) and reduces self-preference bias ([Lifshitz et al., 2025](https://arxiv.org/abs/2502.20379)).

---

## 2. MCP server

```mermaid
flowchart TB
    %% Callers
    IDE["IDE coding agent<br/>(Claude Code, Cursor, Kiro, ...)"]
    Remote["Remote agent /<br/>EKS backend"]

    %% Server process
    subgraph MCPProc["eval-mcp server process"]
        Server["FastMCP server<br/>(eval_mcp/server.py)"]
        Tools["Tool handlers<br/>(eval_mcp/tools/*)"]
        Server --> Tools
    end

    %% Local
    UserDir[("~/.eval-mcp/users/&lt;user&gt;/<br/>configs, datasets, judges, eval logs")]
    Inspect["Inspect AI subprocess<br/>(spawned per eval)"]
    Viewer["Local viewer<br/>(run 'eval-mcp view',<br/>browse :4001)"]

    %% External
    Bedrock[("AWS Bedrock")]
    S3[("Team S3 bucket<br/>(optional)")]

    %% Flow
    IDE -->|"JSON-RPC over stdio"| Server
    Remote -->|"streamable HTTP"| Server

    Tools <-->|"read/write"| UserDir
    Tools -->|"generate_qa, generate_judge,<br/>analyze_*"| Bedrock
    Tools -->|"run_evaluation only"| Inspect

    UserDir --> Viewer
    UserDir <-.->|"auto-sync after<br/>eval-mcp init"| S3
```

**Transport.** `eval_mcp/server.py:main()` reads `EVAL_MCP_TRANSPORT` — defaults to `stdio` (what IDEs use), set to `http` to serve `streamable_http_app` at `EVAL_MCP_PORT` (default 8002) for self-hosted / EKS-sidecar use. Same server, same tools, different mouth.

**Tool registration.** Every tool is registered in `server.py` with a typed signature and an annotation preset (`READ_LOCAL`, `READ_REMOTE`, `CREATE_LOCAL`, `CREATE_REMOTE`, `RUN_REMOTE`). The docstring on the registered function is the description the LLM sees — keep it specific about ID formats, prerequisites, and failure modes.

**Storage.** All persistent state lives under `~/.eval-mcp/users/<user>/` (overridable via `USER_STORAGE_BASE` — the EKS deployment sets this to `/data/users` on an emptyDir mount). Filesystem layout per user: `configs/`, `datasets/`, `judges/`, `logs/`. `EVAL_MCP_USER` (default `local`) selects the user namespace for standalone runs.

**Team sharing.** `eval-mcp init <bucket>` writes the bucket to local config; from then on every write fires `replicate_async` into a thread pool, and every list/read calls `auto_pull` (debounced by TTL) so local state mirrors S3. Account-ID suffix is auto-resolved so teammates type the same short name. Bucket region is auto-detected via `head_bucket` even on cross-region 301 redirects.

**Viewer.** `eval-mcp view` boots a FastAPI app that serves the pre-built Vite/React SPA from `eval_mcp/viewer_static/` (mounts `/assets`, with an `index.html` SPA fallback for client-routed paths). The static bundle is package data per `pyproject.toml`, so rebuilding the frontend (`npm run build:viewer`) affects the published wheel.

**Installers.** `eval_mcp/installers/` has one module per IDE. The dispatcher in `cli.py:install` auto-detects which IDEs are present, asks which to register, and writes the right config in each (JSON merge for Claude Code / Cursor / VS Code / Kiro, TOML round-trip for Codex via `tomlkit` so user comments survive).

---

## 3. EKS deployment

The optional multi-user web app — Cognito-auth'd chat UI for non-technical users. Two Terraform layers with independent state; `deploy.sh` orchestrates both.

```mermaid
flowchart TB
    User["User browser"]
    Edge["AWS edge<br/>(CloudFront + WAF +<br/>oauth2-proxy + Cognito)"]

    subgraph Pods["EKS pods (stateless)"]
        Frontend["Frontend Pod<br/>(static SPA — see note)"]
        subgraph BackendPod["Backend Pod"]
            BE["backend<br/>(FastAPI)"]
            MCP["eval-mcp sidecar"]
            BE <-->|"localhost"| MCP
        end
    end

    subgraph Durable["Durable state"]
        RDS[("RDS Postgres<br/>chat history")]
        S3[("S3 buckets<br/>configs, eval logs,<br/>user uploads")]
    end

    User -->|"HTTPS"| Edge
    Edge --> Frontend
    Edge --> BackendPod
    BE --> RDS
    BE --> S3
    MCP --> S3
```

The `Edge` box collapses several AWS services (CloudFront, WAF, an internal ALB, oauth2-proxy on EKS, Cognito as the IdP) into one logical boundary — they exist, but they're not architecturally interesting beyond "authenticated HTTPS gateway." Same for the two physical S3 buckets and three Bedrock regions, which collapse into single tiles. The depth lives in the prose below.

> **Note — frontend serving (migration in progress).** The frontend is now a static Vite/React SPA (no Node runtime). Locally it's served single-origin by nginx/FastAPI (`make dev`), which is the target EKS shape. The EKS manifests below (the dedicated frontend pod/Deployment, the `/_next/*` ALB rule, CloudFront `/_next/static/*` behavior) still reflect the previous Next.js deployment and are pending the EKS-serving rework: delete the frontend pod and have the backend/edge serve the static bundle, repointing `/_next/*` → `/assets/*`. This was deferred intentionally — it needs a real deploy to verify.

**Two Terraform layers, independent state.**

- `infra/data/` — VPC (2 AZs, public/private/intra subnets, NAT, S3 VPC endpoint), RDS Postgres (db.t3.micro, 20→100GB, IAM auth), two S3 buckets (documents + data). Survives `./destroy.sh`.
- `infra/platform/` — EKS 1.34 (2× t4g.medium managed node group), Karpenter for autoscaling, internal ALB, CloudFront + WAF, Cognito (with optional external OIDC IdP), CodeBuild + ECR, ESO + Pod Identity, multi-region Bedrock logging (`us-west-2`, `us-east-1`, `us-east-2`). Destroyed and recreated by deploy/destroy.

Layers connect via `-var=` flags (not `terraform_remote_state`) so platform state never sees data-state secrets. `deploy.sh` reads ~13 data outputs and passes them in explicitly.

**Why the ALB is internal.** Only CloudFront can reach it, via [VPC Origins](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/private-content-vpc-origins.html) over the AWS private network. The internet never sees the ALB directly — defense in depth on top of WAF.

**Why backend is stateless.** `helm/eval/templates/pvc.yaml` is intentionally empty (see its comment). All durable state goes to S3; pod-local `/data` is `emptyDir`, lost on restart. Chat history is in RDS. This means a pod restart is harmless and HPA scaling Just Works — different from the old EBS-PVC design referenced in some older docs.

**MCP as a sidecar.** The backend Deployment runs two containers in one Pod: `backend` (FastAPI) and `eval-mcp` (HTTP transport on :8002, declared as a K8s 1.28+ native sidecar via `initContainers` with `restartPolicy: Always`). Backend reaches the MCP at `http://localhost:8002/mcp` (the `EVAL_MCP_URL` env var). They share the `/data` emptyDir, so anything the MCP writes is visible to the backend in the same pod.

**Ingress routes** (per `infra/platform/alb.tf` + Helm values):

| Path                  | Service         | Auth                |
|-----------------------|-----------------|---------------------|
| `/`, `/_next/*`       | frontend        | public              |
| `/api/auth/*`         | backend         | oauth2-proxy        |
| `/api/*`              | backend         | oauth2-proxy        |
| `/viewer/*`           | backend         | oauth2-proxy        |
| `/health`             | backend         | public              |
| `/oauth2/*`           | oauth2-proxy    | public              |
| everything else       | frontend        | oauth2-proxy        |

**Secrets.** Everything sensitive is in Secrets Manager; External Secrets Operator syncs it into K8s Secrets via Pod Identity. Database auth uses RDS IAM tokens — no static passwords anywhere.
