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
uv venv
uv pip install -e .
```

`-e` installs editable — edits in `eval_mcp/` take effect on next MCP restart without reinstalling.

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

The viewer is a pre-built Next.js export served by the Python viewer app. When you change `frontend/` source:

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

If you're changing the EKS web app (FastAPI + Next.js chat) and want hot reload for all services:

```bash
AWS_PROFILE=my-profile make dev    # from repo root
```

Opens http://localhost:4001. Each service hot-reloads independently; see [`local/README.md`](../local/README.md) for commands (`make logs`, `make restart s=backend`, etc.) and the architecture diagram.

## Architecture

System architecture, request flow, and security boundaries are in
[ARCHITECTURE.md](../ARCHITECTURE.md) — three mermaid diagrams covering
eval execution, the MCP server, and the EKS deployment. This file
focuses on operational procedures (commands, terraform variables,
troubleshooting) rather than what the system is.

## Project Structure

The repo hosts two deployable things that share some code:

- **`eval_mcp/`** — the MCP package published to PyPI as `llm-evaluation-system`. This is what the README's Install section wires up. Self-contained; no database or web app.
- **`backend/` + `frontend/`** — the full EKS web app (FastAPI + Next.js chat UI + Cognito auth). `./deploy.sh` is its entry point.

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
│   └── viewer_static/            # Pre-built Next.js static export (rebuilt via `npm run build:viewer`)
├── backend/                      # EKS web app (not used by the MCP)
│   ├── api/                      # FastAPI routes (chat, compare, auth)
│   └── core/                     # Chat agent, database, mcp_client
├── frontend/                     # Next.js source for both the web app AND the viewer static export
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

Infrastructure is split into `infra/data/` (persistent: VPC, RDS, S3 — survives `./destroy.sh`) and `infra/platform/` (compute: EKS, ALB, CloudFront, Cognito — recreated by deploy/destroy). Resource breakdowns are in [ARCHITECTURE.md](../ARCHITECTURE.md#3-eks-deployment).

`deploy.sh` reads data-layer outputs and passes them to the platform layer as `-var` flags rather than using `terraform_remote_state` (which would expose the entire data-state including secrets):

```bash
# deploy.sh internally does:
VPC_ID=$(cd infra/data && terraform output -raw vpc_id)
# ... ~13 variables total
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
    ├── deployment.yaml   # Backend Pod (backend + eval-mcp sidecar) + Frontend
    ├── service.yaml      # Backend, Frontend services
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
| ... | | (13 data-layer variables total, passed by deploy.sh) |

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
  # ... (see deploy.sh for all 13 variables)
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
rm -f /tmp/source.zip && zip -r /tmp/source.zip . -x "*.git*" -x "*/node_modules/*" -x "*/.next/*" -x "*.terraform*"
aws s3 cp /tmp/source.zip s3://$(cd infra/platform && terraform output -raw source_bucket)/source.zip
aws codebuild start-build --project-name eval-managed-image-build

# After build completes, restart pods to pull new images
kubectl rollout restart deployment/backend -n eval-managed
kubectl rollout restart deployment/frontend -n eval-managed
```

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
