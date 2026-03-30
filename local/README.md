# Local Deployment

Run the full application locally in a single container. No AWS infrastructure costs — only Bedrock API calls.

## Prerequisites

- **Podman** or **Docker** installed (`brew install podman` or `brew install --cask docker`)
- **AWS credentials** with Bedrock access (SSO, credential_process, static keys, or Bedrock API key)
- **AWS CLI** installed (`brew install awscli`) — used to resolve credentials (not needed with API key)

## Quick Start

```bash
# Using AWS SSO/profile
AWS_PROFILE=my-profile make dev

# Using a Bedrock API key (no IAM credentials needed)
AWS_BEARER_TOKEN_BEDROCK=your-key make dev
```

Open **http://localhost:4001** when it's ready.

## Commands

| Command | Description |
|---------|-------------|
| `make dev` | Dev mode with hot reload (code changes apply instantly) |
| `make run` | Production mode (built assets, runs in background) |
| `make build` | Build container image |
| `make stop` | Stop container |
| `make logs` | Tail container logs |
| `make clean` | Stop and remove build caches (preserves data) |

## What's Running

```
┌─── Single Container ────────────────────────────────┐
│  ├── nginx (reverse proxy)  :4001  ← your browser   │
│  ├── PostgreSQL              :5432                   │
│  ├── Synthetic MCP server    :8002                   │
│  ├── Providers MCP server    :8004                   │
│  ├── Dataset MCP server      :8005                   │
│  ├── Backend (FastAPI)       :8080                   │
│  └── Frontend (Next.js)      :3000                   │
└──────────────────────────────────────────────────────┘
```

nginx routes `/api/*` directly to the backend (preserving SSE streaming) and everything else to the frontend — mirroring the production ALB + oauth2-proxy routing.

## Data Persistence

All data is stored in the `eval-data` volume:
- Chat history (PostgreSQL data)
- Datasets, judges, evaluation configs
- Evaluation results
- Uploaded documents

Data survives container restarts. To reset everything:

```bash
make stop
podman volume rm eval-data  # or: docker volume rm eval-data
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `AWS_PROFILE` | AWS credential profile (for SSO/IAM auth) |
| `AWS_REGION` | AWS region for Bedrock (default: us-west-2) |
| `AWS_BEARER_TOKEN_BEDROCK` | Bedrock API key (alternative to IAM credentials) |

## Troubleshooting

**Build fails with OOM / SIGKILL:**
The Podman VM needs at least 8GB of memory. The Makefile handles Podman clock sync but not memory. Set it manually:
```bash
podman machine stop
podman machine set --memory 8192
podman machine start
```

**Container exits immediately:**
Check logs with `make logs`. Common causes:
- Missing AWS credentials — run `aws sso login` first if using SSO
- Port 4001 already in use — stop whatever is using it

**Bedrock errors:**
Ensure your AWS credentials have `bedrock:InvokeModel` and `bedrock:InvokeModelWithResponseStream` permissions.

**Credentials expired:**
If using SSO or session tokens, credentials are resolved at container start time. Restart to pick up fresh credentials:
```bash
aws sso login  # if using SSO
make stop && make dev
```
