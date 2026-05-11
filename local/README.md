# Local Development

Run the full application locally with Docker Compose. Mirrors the production EKS topology — each service runs in its own container with shared network (like K8s pod sidecars).

## Prerequisites

- **Docker** with Compose V2 (`docker compose`)
- **AWS credentials** with Bedrock access (`aws sso login`)
- **AWS CLI** installed

## Quick Start

```bash
AWS_PROFILE=my-profile make dev
```

Open **http://127.0.0.1:4001** when ready (~2 minutes first build, ~30 seconds after).

## Commands

| Command | Description |
|---------|-------------|
| `make dev` | Start all services with hot reload |
| `make stop` | Stop all services |
| `make logs` | Tail all logs |
| `make logs s=backend` | Tail one service's logs |
| `make restart s=backend` | Restart one service with fresh creds |
| `make build` | Build all images |
| `make clean` | Stop and remove all data volumes |

## Architecture

```
┌─── Docker Compose ──────────────────────────────────────────┐
│                                                              │
│  nginx (:4001) ← your browser                               │
│    ├── /api/* → backend                                      │
│    ├── /inspect/* → backend (Inspect AI viewer)              │
│    └── /* → frontend                                         │
│                                                              │
│  backend  (:8080)        ─┐ shared network (127.0.0.1)       │
│  eval-mcp (:8002)        ─┘ like a K8s pod sidecar            │
│                                                              │
│  frontend (:3000)          separate network                  │
│  postgres (:5432)          separate network                  │
└──────────────────────────────────────────────────────────────┘
```

## Hot Reload

Each service reloads independently:
- Edit `backend/api/` or `backend/core/` → only backend restarts
- Edit `eval_mcp/` → only eval-mcp restarts
- Edit `frontend/` → only frontend reloads
- No cascade crashes

## Credential Refresh

AWS credentials are injected at startup. When they expire:

```bash
aws sso login
make restart s=backend    # restart just backend with fresh creds
```

Or restart everything: `make stop && make dev`

## Data

All data is in Docker volumes:
- `pgdata` — PostgreSQL (chat history)
- `userdata` — user files, eval logs, datasets, judges
- `frontend-nm` — frontend node_modules

Data survives restarts. To reset: `make clean`

## Troubleshooting

**Build fails with ECR 403:**
```bash
aws ecr-public get-login-password --region us-east-1 | docker login --username AWS --password-stdin public.ecr.aws
make dev
```

**Backend won't start (MCP connection error):**
The MCP server needs to be up first. The backend waits for it, but if it fails:
```bash
make logs s=eval-mcp        # check what's wrong
make restart s=backend      # retry after MCP is up
```

**Credentials expired:**
```bash
aws sso login
make restart s=backend
```
