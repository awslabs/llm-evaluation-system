# Development Guide

Technical reference for developers working on the LLM Evaluation Platform.

## Architecture

```
                         ┌─────────────────────────────────────────┐
                         │      CloudFront + WAF → Internal ALB    │
                         └───────────────────┬─────────────────────┘
                                             │
              ┌──────────────────────────────┼──────────────────────────────┐
              │                              │                              │
              ▼                              ▼                              ▼
     ┌─────────────────┐          ┌─────────────────┐          ┌─────────────────┐
     │    Frontend     │          │     Backend     │          │   MCP Servers   │
     │    (Next.js)    │          │    (FastAPI)    │          │  - synthetic    │
     │                 │          │  - Chat/Agent   │          │  - providers    │
     └─────────────────┘          │  - Viewer Proxy │          │  - dataset      │
                                  └────────┬────────┘          └─────────────────┘
                                           │
                    ┌──────────────────────┼──────────────────────┐
                    │                      │                      │
                    ▼                      ▼                      ▼
           ┌──────────────┐       ┌──────────────┐       ┌──────────────┐
           │   User A     │       │   User B     │       │   User C     │
           │ /data/users/ │       │ /data/users/ │       │ /data/users/ │
           │  - configs   │       │  - configs   │       │  - configs   │
           │  - datasets  │       │  - datasets  │       │  - datasets  │
           │  - viewer    │       │  - viewer    │       │  - viewer    │
           └──────────────┘       └──────────────┘       └──────────────┘
                    │                      │                      │
                    └──────────────────────┼──────────────────────┘
                                           │
                              ┌────────────▼────────────┐
                              │   JSON + EBS Storage   │
                              │  Periodic S3 backup      │
                              └─────────────────────────┘
```

## Security Architecture

```
Internet → CloudFront (HTTPS + WAF) → VPC Origin → Internal ALB → Pods
```

- **Internal ALB**: Not internet-facing. Only reachable via CloudFront VPC Origins.
- **VPC Origins**: CloudFront connects to ALB via AWS private network.
- **WAF**: Rate limiting (2000 req/5min per IP), AWS managed rules.
- **Authentication**: Cognito (native or external OIDC IdP) via oauth2-proxy.
- **Secrets**: Stored in AWS Secrets Manager, synced to K8s via External Secrets Operator.
- **Database auth**: IAM token authentication (no static passwords).

## Project Structure

```
llm-evaluation-system/
├── backend/
│   ├── api/           # FastAPI backend (main.py)
│   ├── core/          # Core modules (agent, bedrock_client, database, etc.)
│   └── mcp_servers/   # MCP servers
│       ├── synthetic/ # QA generation, evaluations
│       ├── providers/ # Bedrock model discovery
│       └── dataset/   # Dataset management
├── frontend/          # Next.js chat interface
├── inspect/       # Inspect AI configuration
├── docker/            # Dockerfiles
├── helm/
│   ├── eval/                      # App Helm chart
│   └── external-secrets-config/   # External Secrets configuration
├── infra/
│   ├── data/          # Terraform: persistent layer (VPC, RDS, S3)
│   └── platform/      # Terraform: compute layer (EKS, CloudFront, ALB, etc.)
├── deploy.sh          # One-command deployment
├── destroy.sh         # Teardown preserving data
└── docs/
    └── DEVELOPMENT.md # This file
```

## Infrastructure Layout

Infrastructure is split into two Terraform layers with separate state:

### Data Layer (`infra/data/`)

Persistent resources that survive `./destroy.sh`:

- **VPC** — 10.0.0.0/16 CIDR, 2 AZs, public/private/intra subnets, NAT gateway, S3 VPC endpoint
- **RDS PostgreSQL** — db.t3.micro, 20GB (auto-scales to 100GB), IAM auth enabled
- **S3 Documents Bucket** — User uploads, versioned
- **S3 Backup Bucket** — Periodic JSON backups, versioned

### Platform Layer (`infra/platform/`)

Compute and networking resources destroyed/recreated by scripts:

- **EKS** — Kubernetes 1.34, 2x t4g.medium managed node group (ARM64/Graviton)
- **Karpenter** — Auto-scaling with c/m/r/t instance types (arm64, medium-xlarge)
- **ALB** — Internal (non-internet-facing), targets: backend (8080), frontend (3000), oauth2-proxy (4180)
- **CloudFront** — HTTPS CDN with VPC Origin to internal ALB, HTTP/2+3
- **WAF** — Rate limiting (2000 req/5min per IP), AWS managed rules
- **Cognito** — User Pool with admin-only signup, optional external OIDC IdP
- **CodeBuild** — ARM64 image builds, source from S3
- **ECR** — Private container registry
- **Bedrock Logging** — Multi-region (us-west-2, us-east-1, us-east-2) CloudWatch logs
- **Kubernetes resources** — Namespace, ConfigMap, StorageClass, External Secrets, Pod Identity, TGBs

### How Layers Connect

The deploy script reads data-layer outputs and passes them as `-var` flags to the platform layer. This avoids `terraform_remote_state` (which exposes entire state including secrets).

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
    ├── deployment.yaml   # Backend + Frontend + 3 MCP sidecars + s3-backup
    ├── service.yaml      # Backend, Frontend services
    ├── pvc.yaml          # Backend EBS gp3 storage (200Gi)
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

## Ingress Routes

| Path | Service | Description |
|------|---------|-------------|
| `/api/auth/*` | frontend | NextAuth authentication |
| `/api/*` | backend | FastAPI endpoints |
| `/viewer/*` | backend | Per-user viewer proxy |
| `/health` | backend | Health check |
| `/*` | frontend | Next.js app |

Public routes (no auth): `/`, `/_next/*`, `/favicon.ico` — bypassed at ALB level.
Protected routes: `/chat`, `/api/*`, `/viewer/*` — through oauth2-proxy.

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

Backend uses single-pod architecture with EBS gp3 storage (200Gi). Sidecar backs up JSON databases to S3 every 15 minutes. Brief downtime (~30s) occurs during deploys.

```bash
# Check disk usage
kubectl exec -n eval-managed deployment/backend -- df -h /data

# Expand storage (no downtime, applies automatically)
kubectl patch pvc backend-data -n eval-managed \
  -p '{"spec":{"resources":{"requests":{"storage":"400Gi"}}}}'

# Check backup status
kubectl logs -n eval-managed -l app=backend -c s3-backup
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
