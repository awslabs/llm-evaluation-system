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
    subgraph User["User session (IDE or web chat)"]
        IDE["Coding agent<br/>(Claude Code, Cursor, Kiro, ...)"]
    end

    subgraph MCP["eval-mcp server (stdio or HTTP)"]
        Tools["MCP tools<br/>list_bedrock_models<br/>generate_qa_pairs<br/>generate_judge<br/>create_eval_config<br/>run_evaluation"]
    end

    subgraph Storage["User dir ~/.eval-mcp/users/&lt;user&gt;/"]
        Configs["configs/&lt;name&gt;.py<br/>configs/&lt;name&gt;.json"]
        Datasets["datasets/"]
        Judges["judges/"]
        Logs["logs/&lt;eval_id&gt;.eval<br/>logs/raw_otel/*.jsonl"]
    end

    subgraph Inspect["Inspect AI subprocess<br/>(python -m inspect_ai eval)"]
        Task["Task: samples × target models"]
        Solver["Solver<br/>standard: call target model directly<br/>agent: spawn agent subprocess"]
        Scorer["Jury scorer<br/>N judge models score each sample<br/>binary per-criterion, majority vote"]
    end

    subgraph Capture["Agent-eval capture path"]
        AgentProc["Agent subprocess<br/>(user's agent_path)"]
        OTLP["In-harness OTLP receiver<br/>(eval_mcp/otlp_receiver.py)"]
    end

    Bedrock[("AWS Bedrock<br/>(target + judge models)")]
    Viewer["Local viewer :4001<br/>eval_mcp/viewer.py + viewer_static/"]
    Report["PDF report<br/>generate_report"]

    IDE -->|"tool call"| Tools
    Tools -->|"writes"| Configs
    Tools -->|"writes"| Datasets
    Tools -->|"writes"| Judges

    Tools -->|"spawn (asyncio.create_subprocess_exec)"| Inspect
    Inspect --> Task
    Task --> Solver
    Solver -->|"standard eval"| Bedrock
    Solver -->|"agent eval: spawn"| AgentProc
    AgentProc -->|"OTEL_EXPORTER_OTLP_ENDPOINT"| OTLP
    AgentProc -->|"instrumented Bedrock calls"| Bedrock
    OTLP -->|"spans → ModelEvents"| Solver
    Solver --> Scorer
    Scorer -->|"each judge calls Bedrock<br/>per criterion"| Bedrock
    Scorer -->|"writes .eval log"| Logs

    Tools -->|"read .eval log"| Logs
    Tools --> Report
    Logs --> Viewer
    IDE -->|"open browser"| Viewer
```

**Tool order in a typical session:** `list_bedrock_models` → `generate_qa_pairs` (from docs or context) → `save_dataset` → `generate_judge` → `create_eval_config` → `run_evaluation` → `generate_report`. The agent in the IDE picks the order; the MCP just exposes the tools.

**Why subprocess isolation.** `run_evaluation` shells out to `python -m inspect_ai eval` rather than calling Inspect in-process. A cancelled or crashed eval can't take down the MCP, and the subprocess gets a fresh interpreter so OTel instrumentation can be installed cleanly per run.

**How agent evals capture Bedrock calls.** For `agent_path` configs, the solver spawns the agent as a subprocess with `opentelemetry-instrument` autoloaded (via `opentelemetry-distro`) and `OTEL_EXPORTER_OTLP_ENDPOINT` pointed at an in-process OTLP receiver inside the Inspect subprocess. The agent's Bedrock calls emit spans → receiver → ModelEvents in the `.eval` log. A pre-flight canary in `eval_mcp/canary.py` exercises this path once before the real eval, so a broken capture pipeline fails loudly instead of returning `success=true, scores=[]`. Raw spans are also appended to `logs/raw_otel/<eval_id>.jsonl` as cold storage in case the projection ever drops data.

**Jury scoring.** Multiple judges from different model families (default in `eval_mcp/core/judge_config.py`) each score every sample binary-per-criterion. `backend/core/jury_scoring.py` aggregates: majority vote per criterion, then sample passes if all criteria pass. This is more reliable than single-judge numeric scales ([Mallinar et al., 2025](https://arxiv.org/abs/2503.23339v2)) and reduces self-preference bias ([Lifshitz et al., 2025](https://arxiv.org/abs/2502.20379)).

---

## 2. MCP server

```mermaid
flowchart TB
    subgraph Clients["Clients"]
        CC["Claude Code / Cursor / Kiro / VS Code / Codex<br/>(stdio)"]
        Remote["Remote agent / EKS backend<br/>(streamable HTTP)"]
    end

    subgraph Process["eval-mcp process"]
        CLI["eval_mcp/cli.py<br/>(click group)"]
        Server["eval_mcp/server.py<br/>FastMCP('eval-server')"]

        subgraph ToolMods["eval_mcp/tools/"]
            T1["dataset: generate_qa, save_dataset, list_datasets"]
            T2["judge: generate_judge, list_judges"]
            T3["config: create_eval_config, create_agent_eval_config"]
            T4["run: run_evaluation, retry_evaluation, optimize_prompt"]
            T5["read: list_evaluations, get_evaluation_details, analyze_*"]
            T6["report: generate_report"]
        end

        subgraph Core["eval_mcp/core/"]
            Bed["bedrock_client.py<br/>cross-region + API-key auth"]
            UStore["user_storage.py<br/>get_user_dir / get_user_log_dir"]
            Pricing["pricing.py + provider_pricing.json"]
            Jury["judge_config.py"]
        end

        Sub["subprocess_runner.py<br/>+ _agent_launcher.py<br/>(spawns inspect-ai)"]
        OTLP2["otlp_receiver.py +<br/>bedrock_capture.py"]
        S3Sync["s3_sync.py<br/>(replicate_async, auto_pull,<br/>sync_up/down/to_project)"]
        Viewer2["viewer.py<br/>FastAPI on :4001<br/>serves viewer_static/"]

        Installers["installers/<br/>claude_code, codex, cursor, kiro, vscode"]
    end

    UserDir[("~/.eval-mcp/users/&lt;user&gt;/<br/>configs, datasets, judges, logs<br/>USER_STORAGE_BASE overrides")]
    BedrockSvc[("AWS Bedrock")]
    S3Bucket[("S3 team bucket<br/>(optional)<br/>users/&lt;you&gt;/, projects/&lt;name&gt;/")]
    Browser["Browser"]

    CC -->|"JSON-RPC over stdio"| Server
    Remote -->|"streamable_http_app"| Server
    CLI -->|"no subcommand → run server"| Server
    CLI -->|"eval-mcp install"| Installers
    CLI -->|"eval-mcp init &lt;bucket&gt;"| S3Sync
    CLI -->|"eval-mcp view"| Viewer2
    CLI -->|"eval-mcp sync / share"| S3Sync

    Server --> ToolMods
    ToolMods --> Core
    ToolMods --> Sub
    Sub -.->|"OTel spans"| OTLP2
    Core --> BedrockSvc
    ToolMods <--> UserDir
    UserDir -.->|"auto-replicate on write"| S3Sync
    S3Sync <--> S3Bucket
    UserDir -.->|"auto-pull on list/read (debounced)"| S3Sync

    Viewer2 --> UserDir
    Browser --> Viewer2
```

**Transport.** `eval_mcp/server.py:main()` reads `EVAL_MCP_TRANSPORT` — defaults to `stdio` (what IDEs use), set to `http` to serve `streamable_http_app` at `EVAL_MCP_PORT` (default 8002) for self-hosted / EKS-sidecar use. Same server, same tools, different mouth.

**Tool registration.** Every tool is registered in `server.py` with a typed signature and an annotation preset (`READ_LOCAL`, `READ_REMOTE`, `CREATE_LOCAL`, `CREATE_REMOTE`, `RUN_REMOTE`). The docstring on the registered function is the description the LLM sees — keep it specific about ID formats, prerequisites, and failure modes.

**Storage.** All persistent state lives under `~/.eval-mcp/users/<user>/` (overridable via `USER_STORAGE_BASE` — the EKS deployment sets this to `/data/users` on an emptyDir mount). Filesystem layout per user: `configs/`, `datasets/`, `judges/`, `logs/`. `EVAL_MCP_USER` (default `local`) selects the user namespace for standalone runs.

**Team sharing.** `eval-mcp init <bucket>` writes the bucket to local config; from then on every write fires `replicate_async` into a thread pool, and every list/read calls `auto_pull` (debounced by TTL) so local state mirrors S3. Account-ID suffix is auto-resolved so teammates type the same short name. Bucket region is auto-detected via `head_bucket` even on cross-region 301 redirects.

**Viewer.** `eval-mcp view` boots a FastAPI app that serves the pre-built Next.js export from `eval_mcp/viewer_static/`. The static bundle is package data per `pyproject.toml`, so rebuilding the frontend (`npm run build:viewer`) affects the published wheel.

**Installers.** `eval_mcp/installers/` has one module per IDE. The dispatcher in `cli.py:install` auto-detects which IDEs are present, asks which to register, and writes the right config in each (JSON merge for Claude Code / Cursor / VS Code / Kiro, TOML round-trip for Codex via `tomlkit` so user comments survive).

---

## 3. EKS deployment

The optional multi-user web app — Cognito-auth'd chat UI for non-technical users. Two Terraform layers with independent state; `deploy.sh` orchestrates both.

```mermaid
flowchart TB
    User["User browser"]

    subgraph Edge["AWS edge"]
        CF["CloudFront (HTTPS)<br/>HTTP/2+3"]
        WAF["WAF<br/>rate-limit 2000 req/5min<br/>AWS managed rules"]
        VPCOrigin["VPC Origin<br/>(private AWS network)"]
    end

    Cognito["Cognito User Pool<br/>admin-signup + optional OIDC IdP"]

    subgraph VPC["VPC (infra/data) — 10.0.0.0/16"]
        ALB["Internal ALB<br/>(not internet-facing)"]
        S3End["S3 VPC Endpoint"]

        subgraph EKS["EKS cluster (infra/platform)"]
            direction TB
            OAuth["oauth2-proxy<br/>:4180"]

            subgraph BackendPod["Pod: backend"]
                BE["backend container<br/>FastAPI :8080"]
                EvalSidecar["eval-mcp sidecar<br/>HTTP :8002<br/>(K8s 1.28+ native sidecar)"]
                Eph["emptyDir /data<br/>USER_STORAGE_BASE=/data/users"]
            end

            subgraph FrontendPod["Pod: frontend"]
                FE["Next.js :3000"]
            end

            Karp["Karpenter<br/>arm64 c/m/r/t medium-xl"]
            ESO["External Secrets Operator<br/>+ Pod Identity"]
        end
    end

    subgraph DataLayer["infra/data (persistent)"]
        RDS[("RDS Postgres<br/>chat history<br/>IAM auth")]
        S3Docs[("S3 documents bucket<br/>user uploads")]
        S3Data[("S3 data bucket<br/>configs, datasets, judges,<br/>logs, periodic JSON backup")]
    end

    subgraph CICD["Image pipeline (infra/platform)"]
        CB["CodeBuild<br/>(ARM64 build)"]
        ECR[("ECR<br/>backend + frontend images")]
        SrcS3[("S3 source bucket")]
    end

    SM[("Secrets Manager<br/>Cognito client + cookie + DB IAM")]
    BedrockSvc2[("AWS Bedrock<br/>multi-region inference<br/>+ CloudWatch logging")]

    User -->|"HTTPS"| CF
    CF --> WAF
    WAF --> VPCOrigin
    VPCOrigin --> ALB

    ALB -->|"/oauth2/*, protected routes"| OAuth
    ALB -->|"/api/*"| BE
    ALB -->|"/viewer/*"| BE
    ALB -->|"/*"| FE
    OAuth -.->|"login redirect"| Cognito
    Cognito -.->|"OIDC"| OAuth

    BE <-->|"http://localhost:8002/mcp"| EvalSidecar
    BE --> Eph
    EvalSidecar --> Eph
    BE -->|"chat history"| RDS
    BE -->|"user uploads"| S3Docs
    EvalSidecar -->|"durable state +<br/>periodic backup"| S3Data
    EvalSidecar -->|"target + judge calls"| BedrockSvc2

    Eph -.->|"ephemeral; lost on pod restart"| Eph
    S3Data -.->|"S3 VPC endpoint"| S3End

    ESO --> SM
    SM -.->|"injected as K8s secrets"| BE
    SM -.->|"injected"| OAuth

    SrcS3 --> CB
    CB --> ECR
    ECR -.->|"image pull"| BackendPod
    ECR -.->|"image pull"| FrontendPod

    Karp -.->|"provisions nodes"| BackendPod
    Karp -.->|"provisions nodes"| FrontendPod
```

**Two Terraform layers, independent state.**

- `infra/data/` — VPC (2 AZs, public/private/intra subnets, NAT, S3 VPC endpoint), RDS Postgres (db.t3.micro, 20→100GB, IAM auth), two S3 buckets (documents + data). Survives `./destroy.sh`.
- `infra/platform/` — EKS 1.34 (2× t4g.medium managed node group), Karpenter for autoscaling, internal ALB, CloudFront + WAF, Cognito (with optional external OIDC IdP), CodeBuild + ECR, ESO + Pod Identity, multi-region Bedrock logging (`us-west-2`, `us-east-1`, `us-east-2`). Destroyed and recreated by deploy/destroy.

Layers connect via `-var=` flags (not `terraform_remote_state`) so platform state never sees data-state secrets. `deploy.sh` reads ~13 data outputs and passes them in explicitly.

**Why the ALB is internal.** Only CloudFront can reach it, via [VPC Origins](https://aws.amazon.com/cloudfront/vpc-origins/) over the AWS private network. The internet never sees the ALB directly — defense in depth on top of WAF.

**Why backend is stateless.** `helm/eval/templates/pvc.yaml` is intentionally empty (see its comment). All durable state goes to S3; pod-local `/data` is `emptyDir`, lost on restart. Chat history is in RDS. This means a pod restart is harmless and HPA scaling Just Works — different from the old EBS-PVC design referenced in some older docs.

**MCP as a sidecar.** The backend Deployment runs two containers in one Pod: `backend` (FastAPI) and `eval-mcp` (HTTP transport on :8002, declared as a K8s 1.28+ native sidecar via `initContainers` with `restartPolicy: Always`). Backend reaches the MCP at `http://localhost:8002/mcp` (the `EVAL_MCP_URL` env var). They share the `/data` emptyDir, so anything the MCP writes is visible to the backend in the same pod.

**Ingress routes** (per `infra/platform/alb.tf` + Helm values):

| Path                  | Service         | Auth                |
|-----------------------|-----------------|---------------------|
| `/`, `/_next/*`       | frontend        | public              |
| `/api/auth/*`         | frontend        | public (NextAuth)   |
| `/api/*`              | backend         | oauth2-proxy        |
| `/viewer/*`           | backend         | oauth2-proxy        |
| `/health`             | backend         | public              |
| `/oauth2/*`           | oauth2-proxy    | public              |
| everything else       | frontend        | oauth2-proxy        |

**Secrets.** Everything sensitive is in Secrets Manager; External Secrets Operator syncs it into K8s Secrets via Pod Identity. Database auth uses RDS IAM tokens — no static passwords anywhere.
