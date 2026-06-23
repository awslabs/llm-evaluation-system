# Development Guide

Technical reference for developers working on the LLM Evaluation Platform.

## Working on the MCP locally

If you're changing eval_mcp code (tools, server, viewer) and want to run your changes against your own IDE.

### Dev loop at a glance

End-users install via `uvx --from llm-evaluation-system eval-mcp` — do **not** iterate against that, it pulls the published package from uvx's cache. For development, use these two layers:

1. **pytest** against narrow, deterministic logic (regex, parsing, validation) — milliseconds, catches a specific class of regression cheaply. Not a substitute for end-to-end coverage because the tools spawn real subprocesses, call Bedrock, and write user dirs — mocks give false green builds.
2. **Claude Code / Desktop** pointed at your local build — the primary integration test. This is the ground truth: you exercise the exact same path users will get when you publish. Plan for this to be the main way you verify changes.

### 1. Clone and install editable

```bash
git clone https://github.com/awslabs/llm-evaluation-system.git
cd llm-evaluation-system
uv sync
```

`uv sync` installs editable from `uv.lock` — the **same** pinned versions the deployed EKS image builds with (`uv sync --locked` in the `Dockerfile`), so your local inspect-ai matches production exactly rather than floating to whatever PyPI's latest is. Edits in `eval_mcp/` take effect on next MCP restart without reinstalling. After changing a dependency in `pyproject.toml`, run `uv lock` to refresh the lockfile, then `uv sync` again.

### 2. Pytest for narrow deterministic logic (optional supplement)

For pure functions (regex parsing, validation, config munging), a handler-level pytest is a cheap safety net. The official MCP servers repo (`github.com/modelcontextprotocol/servers`) imports handlers directly — no server boot, no transport mocking. Same pattern here:

```python
# tests/test_run_eval.py
from eval_mcp.tools.run_eval import _validate_providers

async def test_invalid_bedrock_id_fails_validation():
    result = await _validate_providers(["bedrock/nonexistent-model"])
    assert result["valid"] is False
    assert "Invalid model ID" in result["failed_providers"][0]["error"]
```

Run with `.venv/bin/pytest tests/`. Useful for pinning down specific regressions after you hit them — not useful as a proof that a feature works end-to-end.

### 3. Point Claude Code at your local build (permanent dev setup)

**Mental model:** as a maintainer you want your IDE running *your* code, not PyPI's. Published users get the uvx install; you get the editable install. This divergence is the point — it's how you see unreleased changes before shipping them. Same pattern as any Python package: `pip install -e .` locally, published version for everyone else.

**Configure it once:** edit `~/.claude.json` and change the `eval` MCP entry so `command` points at your editable install's binary. Leave it this way for as long as you're developing on the MCP. No swap-back-when-done step.

```json
"eval": {
  "type": "stdio",
  "command": "/absolute/path/to/llm-evaluation-system/.venv/bin/eval-mcp",
  "env": {}
}
```

**Your dev loop after that:**

1. Edit code in `eval_mcp/*.py`.
2. `/mcp` → reconnect `eval`. That restarts the subprocess, which picks up your edits via the editable install. No reinstall, no IDE restart.
3. Call the tool and verify behavior.

**That's the entire loop.** Reconnect is your "refresh from local." You don't need a dual-entry `eval` + `eval-dev` setup — one entry, pointed at local, is the simplest thing that works and matches Anthropic's MCP quickstart pattern for developing servers.

**When you ship a release**, users on `uvx --from llm-evaluation-system eval-mcp` pick it up when they run `uv cache clean`. Your local setup is unaffected — you stay on whatever is in your working tree. If you need to sanity-check the published version behaves as expected (rare), swap the entry temporarily back to `uvx --from llm-evaluation-system eval-mcp` or run `uvx --from llm-evaluation-system eval-mcp --help` in a terminal.

### 4. Rebuild the viewer frontend

The viewer is a pre-built Vite/React SPA served by the Python viewer app. When you change `frontend/` source:

```bash
cd frontend
npm install         # first time only
npm run build:viewer
```

This compiles the frontend and copies the static output into `eval_mcp/viewer_static/`. The viewer picks it up on next `eval-mcp view`.

### 5. Running evals against your changes

```bash
.venv/bin/eval-mcp view              # results viewer on :4001
.venv/bin/inspect eval <task.py>     # run an eval directly via Inspect AI CLI
```

### 6. Publishing a new version

Releases are **tag-triggered via GitHub Actions** (`.github/workflows/publish.yml`). When you push a tag matching `v*`, CI rebuilds the viewer frontend, builds the Python wheel, verifies the tag matches `pyproject.toml`, and publishes to PyPI via trusted publishing (OIDC — no token juggling).

**Release steps:**

```bash
# 1. Bump version in pyproject.toml (patch/minor/major per SemVer)
# 2. Sync the lockfile
uv lock

# 3. Commit the version bump + lock + any release-note changes
git add pyproject.toml uv.lock
git commit -m "Release vX.Y.Z: <summary>"

# 4. Push + tag. CI takes over.
git push origin main
git tag vX.Y.Z
git push origin vX.Y.Z
```

Watch the release run:
```bash
gh run list --workflow=publish.yml --limit 1
gh run watch <run-id>
```

Verify from a clean environment after the run completes:
```bash
uvx --refresh --from 'llm-evaluation-system==X.Y.Z' eval-mcp --help
```

**Release discipline:**

- Users install via plain `uvx --from llm-evaluation-system eval-mcp` (cached per user). They won't pick up a new release until they explicitly run `uv cache clean llm-evaluation-system` — some natural insulation, but assume any published version can reach users at any time.
- Every main-branch commit should be releasable. Land behind a CI gate (pytest + lint + type-check).
- Bump the SemVer appropriately: patch for bug fixes, minor for additive changes, major for breaking tool-signature changes.
- Do **not** instruct users to put `@latest` in their IDE config — it forces PyPI resolution on every MCP start (~20s cold start) and causes "disconnected" state on first connect after every release. See README "Upgrading" for the correct upgrade path.
- `uv publish` locally is possible but not recommended — it bypasses the CI viewer rebuild and skips the tag-version-matches check. Only reach for it in emergencies.

### 7. Adding a new tool (checklist)

1. Implement the handler in `eval_mcp/tools/<name>.py` as an async function.
2. Register it in `eval_mcp/server.py` with a typed signature. The docstring is the tool description the LLM sees — keep it specific about expected ID formats, required prerequisites, and failure modes.
3. (Optional) Write a pytest case for any narrow deterministic logic (parsing, validation) so regressions get caught cheaply next time.
4. Point Claude Code at your local build (see section 3) and exercise the tool end-to-end through the IDE before publishing.

### 8. Running the MCP in Docker (optional)

The repo root `Dockerfile` builds a slim container that runs `eval-mcp serve` as an HTTP MCP (for self-hosting on EC2/ECS/AgentCore). Local dev rarely needs this — use the editable install above.

```bash
docker build -t eval-mcp .
docker run -p 8002:8002 \
  -e AWS_REGION=us-west-2 \
  -v ~/.aws:/root/.aws:ro \
  eval-mcp
```

MCP listens at `http://localhost:8002/mcp`.

## Full web app locally (Docker Compose)

If you're changing the EKS web app (FastAPI + Vite/React chat):

```bash
AWS_PROFILE=my-profile make dev    # from repo root: builds the SPA, then docker compose
```

Opens http://localhost:4001. `make dev` builds the static SPA into `frontend/dist`, which nginx serves directly and proxies the gated paths (`/api`, `/inspect`) to the backend (no Node frontend container — nginx stands in for the EKS edge, where CloudFront serves the SPA from a private S3 bucket and routes the gated paths to the ALB/oauth2-proxy/backend). The backend hot-reloads on Python edits; for frontend edits rerun `make dev-spa` and refresh. See [`local/README.md`](../local/README.md) for commands (`make logs`, `make restart s=backend`, etc.) and the architecture diagram.

## How to *really* test the web app (the layers that count)

Pytest mocks produce false greens for anything touching Bedrock, the MCP subprocess, the agent loop, or per-user storage (same caveat as the MCP — see section 2). For the web app, "tested" means **exercised against the live `make dev` stack**, layered cheapest-first. Use the right layer for the surface you changed:

| Layer | Tool | What it proves | When |
|---|---|---|---|
| 1. Pure logic | `pytest tests/` | deny-by-default decisions, path-boundary math, parsing | authz resolvers, validators, regex — anything deterministic |
| 2. HTTP/API E2E | `curl`/`urllib` against `:4001` (or backend `:8080` direct) | real routes, real DB, real auth-gating | any `/api/*` route; the `verify_*.py` scripts are the model |
| 3. **Agent/chat E2E** | `curl` the **`/api/chat/message`** endpoint (`{"stream":false}`) | the **model actually invokes the MCP tool** and the result flows back | anything the chat agent drives — MCP tools, tool-arg injection, shared-eval surfacing |
| 4. Browser UI | **`webapp-testing` skill (Playwright)** | components render, forms submit, modals/nav work | any change the SPA renders — pages, modals, badges, columns |

**Key distinction (don't conflate layers 3 and 4):**
- **Chat behavior is layer 3, not Playwright.** To prove the agent really lists/reads what it should, POST to `/api/chat/message` with `{"stream": false}` and assert on the model's reply. Example — proving an eval surfaces in chat only when it should:
  ```bash
  # turn 1 (no grant): the agent must NOT see it
  curl -s -X POST http://localhost:4001/api/chat/message -H 'Content-Type: application/json' \
    -d '{"message":"List my evaluations. Is run id FOO123 present? yes/no.","stream":false}' \
    | python3 -c 'import sys,json;print(json.load(sys.stdin)["response"])'
  # ...create the grant, then re-ask in a fresh turn → the agent now names it.
  ```
  This needs Bedrock creds (the agent calls Bedrock) — `make dev` already exports them from your AWS profile, so it Just Works; it is **not** a blocker.
- **Two identities locally:** the nginx stub pins every `:4001` request to `local-user`. To act as a second user, hit the backend container directly with your own header — `docker compose -f local/compose.yaml exec -T backend curl ... -H 'X-Forwarded-User: other-user' http://localhost:8080/...` — or plant the other user's data/grants under `/data/users/<id>/` and via `psql`. See [`verify_grant_isolation.py`](../verify_grant_isolation.py) and [`verify_tenant_isolation.py`](../verify_tenant_isolation.py) for the full pattern (plant → assert deny → grant → assert allow → revoke → assert deny).
- **Browser UI is layer 4 — the `webapp-testing` skill.** It spins up Playwright headless and clicks through real pages (`:4001`). Use it for what the user *sees*; use layer 3 for what the agent *does*.
- **Clean up after E2E:** these layers write real rows/files. Remove planted `/data/users/<id>/` dirs and `psql DELETE` test grants/teams/users in a `finally`, and verify zero leftovers (the `verify_*.py` scripts do this).

**Deployed verification (real AWS):** after `./deploy.sh`, the same layers apply, plus in-pod checks via `kubectl exec -n eval-managed <backend-pod> -c backend -- python3 -c '...'` against production RDS (read/describe first; clean up any test rows). CI runs none of this — these are manual, run them before shipping anything non-trivial.

## Architecture

System architecture, request flow, and security boundaries are in
[ARCHITECTURE.md](../ARCHITECTURE.md) — three mermaid diagrams covering
eval execution, the MCP server, and the EKS deployment. This file
focuses on operational procedures (commands, terraform variables,
troubleshooting) rather than what the system is.

## Project Structure

The repo hosts two deployable things that share some code:

- **`eval_mcp/`** — the MCP package published to PyPI as `llm-evaluation-system`. This is what the README's Install section wires up. Self-contained; no database or web app.
- **`backend/` + `frontend/`** — the full EKS web app (FastAPI + Vite/React chat UI + Cognito auth). `./deploy.sh` is its entry point.

```
llm-evaluation-system/
├── eval_mcp/                     # MCP package (published to PyPI)
│   ├── cli.py                    # `eval-mcp` CLI entry point
│   ├── server.py                 # FastMCP server registering all tools
│   ├── core/                     # Shared utilities (bedrock client, storage, pricing, ...)
│   ├── tools/                    # MCP tool handlers (run_eval, generate_qa, ...)
│   ├── bedrock_capture.py        # OTel-based Bedrock call capture for agent evals
│   ├── s3_sync.py                # Optional team-sharing via S3
│   ├── viewer.py                 # Local results viewer (FastAPI)
│   └── viewer_static/            # Pre-built Vite/React SPA bundle (rebuilt via `npm run build:viewer`)
├── backend/                      # EKS web app (not used by the MCP)
│   ├── api/                      # FastAPI routes (chat, compare, auth)
│   └── core/                     # Chat agent, database, mcp_client
├── frontend/                     # Vite/React SPA source for both the web app AND the viewer static export
├── docker/                       # Dockerfiles (web app + MCP serve mode)
├── helm/                         # EKS Helm chart + external-secrets config
├── infra/
│   ├── data/                     # Terraform: persistent layer (VPC, RDS, S3) for the web app
│   ├── platform/                 # Terraform: compute layer (EKS, CloudFront, ALB) for the web app
│   └── eval-logs-bucket/         # Terraform: optional S3 bucket for MCP team sharing
├── deploy.sh / destroy.sh        # Web-app deployment
├── manage-users.sh               # Web-app Cognito user admin
└── docs/DEVELOPMENT.md           # This file
```

The EKS/web-app content below is the heavyweight deployment path. For the MCP, the section above ("Working on the MCP locally") is what you want.

## How the Terraform layers connect

Infrastructure is split into `infra/data/` (persistent: VPC, RDS, S3 buckets incl. the private SPA bucket, and the **Cognito user pool** — survives `./destroy.sh`) and `infra/platform/` (compute: EKS, ALB, CloudFront + SPA OAC/origin, the Cognito **client** — recreated by deploy/destroy). The Cognito *pool* is in the data layer so a platform teardown preserves user accounts; the *client* (which references CloudFront) is per-deployment. Resource breakdowns are in [ARCHITECTURE.md](../ARCHITECTURE.md#3-eks-deployment).

`deploy.sh` reads data-layer outputs and passes them to the platform layer as `-var` flags rather than using `terraform_remote_state` (which would expose the entire data-state including secrets):

```bash
# deploy.sh internally does:
VPC_ID=$(cd infra/data && terraform output -raw vpc_id)
# ... ~19 data-layer outputs total (VPC, RDS, S3 buckets, Cognito pool)
cd infra/platform && terraform apply -var="vpc_id=$VPC_ID" ...
```

## OIDC Identity Provider Configuration

To use an external IdP (Okta, Azure AD, etc.) instead of Cognito native auth, edit `infra/platform/terraform.tfvars` before deploying:

```hcl
enable_oidc_idp        = true
oidc_provider_name     = "YourIdP"
oidc_client_id         = "your-client-id"
oidc_client_secret_arn = "arn:aws:secretsmanager:..."
oidc_issuer_url        = "https://your-idp.example.com"
```

## Helm Chart Structure

```
helm/eval/
├── Chart.yaml            # Chart metadata (depends on oauth2-proxy subchart)
├── values.yaml           # Shared defaults (multi-environment)
├── values-aws.yaml       # AWS EKS overrides
└── templates/
    ├── deployment.yaml   # Backend Pod (backend + eval-mcp sidecar). No frontend pod — SPA is on S3/CloudFront.
    ├── service.yaml      # Backend service
    ├── pvc.yaml          # Intentionally empty — backend is stateless (data in S3)
    ├── hpa.yaml          # Horizontal Pod Autoscaling
    ├── pdb.yaml          # Pod Disruption Budgets
    └── rbac.yaml         # RBAC configuration
```

Helm `--set` values required for AWS:
- `aws.region` — AWS region
- `aws.accountId` — AWS account ID
- `projectName` — Project name with region suffix (e.g., `eval-managed-uswest2`)

## Terraform Variables

### Data Layer

| Variable | Default | Description |
|----------|---------|-------------|
| `region` | `us-west-2` | AWS region |
| `project_name` | `eval-managed` | Resource name prefix |

### Platform Layer

Inherits `region` and `project_name`, plus:

| Variable | Default | Description |
|----------|---------|-------------|
| `bedrock_cross_regions` | `["us-east-1", "us-east-2"]` | Regions for Bedrock cross-region inference logging |
| `eks_cluster_version` | `1.34` | Kubernetes version |
| `cluster_admin_role_arns` | `[]` | Additional IAM roles for EKS admin |
| `enable_oidc_idp` | `false` | Use external OIDC identity provider |
| `oidc_provider_name` | `ExternalOIDC` | OIDC provider name in Cognito |
| `oidc_client_id` | `""` | OIDC client ID |
| `oidc_client_secret_arn` | `""` | Secrets Manager ARN for OIDC client secret |
| `oidc_issuer_url` | `""` | OIDC issuer URL |
| `vpc_id` | (required) | From data layer output |
| `private_subnets` | (required) | From data layer output |
| ... | | (~19 data-layer outputs total — VPC, RDS, S3 buckets, Cognito pool — passed by deploy.sh) |

## Manual Deployment Steps

For developers who want to run each phase individually instead of using `./deploy.sh`:

### 1. Deploy Infrastructure

```bash
cd infra/data

# Create terraform.tfvars with your region
echo 'region = "us-west-2"' > terraform.tfvars

terraform init
terraform apply

cd ../platform

# Create terraform.tfvars with any overrides (optional — defaults work for most setups)
# See infra/platform/variables.tf for all available options
echo 'region = "us-west-2"' > terraform.tfvars

terraform init
terraform apply \
  -var="vpc_id=$(cd ../data && terraform output -raw vpc_id)" \
  -var="private_subnets=$(cd ../data && terraform output -json private_subnets)" \
  # ... (see deploy.sh for all ~19 data-layer outputs)
```

### 2. Configure kubectl

```bash
aws eks update-kubeconfig --name eval-managed --region us-west-2
```

### 3. Build and Deploy Application

```bash
# Upload source and build images
rm -f /tmp/source.zip && zip -r /tmp/source.zip . -x "*.git*" -x "*/node_modules/*" -x "*/.next/*" -x "*.terraform*"
aws s3 cp /tmp/source.zip s3://$(cd infra/platform && terraform output -raw source_bucket)/source.zip
aws codebuild start-build --project-name $(cd infra/platform && terraform output -raw codebuild_project)

# Check build status
aws codebuild list-builds-for-project \
  --project-name eval-managed-image-build \
  --query 'ids[0]' --output text | \
  xargs -I {} aws codebuild batch-get-builds --ids {} \
  --query 'builds[0].buildStatus' --output text

# Deploy with Helm
export AWS_REGION=us-west-2
export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGION_SUFFIX=$(echo $AWS_REGION | tr -d '-')

helm upgrade --install eval ./helm/eval -n eval-managed \
  -f ./helm/eval/values-aws.yaml \
  --set aws.region=$AWS_REGION \
  --set aws.accountId=$AWS_ACCOUNT_ID \
  --set projectName=eval-managed-${REGION_SUFFIX}
```

### 4. Updating the App

After code changes:

```bash
# Upload and rebuild
rm -f /tmp/source.zip && zip -r /tmp/source.zip . -x "*.git*" -x "*/node_modules/*" -x "*.terraform*"
aws s3 cp /tmp/source.zip s3://$(cd infra/platform && terraform output -raw source_bucket)/source.zip
aws codebuild start-build --project-name eval-managed-image-build

# After build completes, restart the backend to pull the new image
kubectl rollout restart deployment/backend -n eval-managed
```

In practice just re-run `./deploy.sh` — it rebuilds the backend image, **republishes
the SPA to S3** (`npm run build` → `aws s3 sync` → CloudFront invalidation), and
rolls the backend. There is no frontend deployment to restart; frontend changes
ship via the S3 sync, not a pod.

## Troubleshooting

### Pods Not Starting

```bash
kubectl describe pod -l app=backend -n eval-managed
kubectl logs -l app=backend -n eval-managed --previous
```

### View Helm Values

```bash
# See what values are being used
helm get values eval -n eval-managed

# See rendered templates
helm template eval ./helm/eval -f ./helm/eval/values-aws.yaml \
  --set aws.region=$AWS_REGION --set aws.accountId=$AWS_ACCOUNT_ID
```

### Storage Management

Backend is **stateless** — `/data` is an `emptyDir` volume (lost on pod restart). All durable state lives in S3:

- **S3 data bucket** (`infra/data/storage.tf` → `data_bucket`) — configs, datasets, judges, eval logs, PDF reports. Written directly by `eval-mcp` via `USER_STORAGE_BASE=/data/users` plus the S3 sync layer.
- **S3 documents bucket** — user-uploaded PDFs and knowledge bases.
- **RDS Postgres** — chat history.

Pod restarts and rolling deploys are therefore non-disruptive to durable state. HPA scaling works without volume coordination. There is no PVC and no `s3-backup` sidecar; `helm/eval/templates/pvc.yaml` is intentionally empty (see its comment).

```bash
# Inspect the ephemeral working dir (resets on pod restart)
kubectl exec -n eval-managed deployment/backend -c backend -- df -h /data

# Verify durable state is in S3
aws s3 ls s3://$(cd infra/data && terraform output -raw data_bucket)/users/

# Tail MCP sidecar logs (where the S3 writes happen)
kubectl logs -n eval-managed -l app=backend -c eval-mcp
```

### Terraform State

Each layer has independent state in its own directory:

```bash
# Data layer state
cd infra/data && terraform state list

# Platform layer state
cd infra/platform && terraform state list
```

### Docker Rebuild Required For

- Dockerfile changes
- New Python dependencies (pyproject.toml)
