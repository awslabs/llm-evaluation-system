# Self-host eval-mcp

Run the MCP server on a shared host (EC2, EKS, AgentCore, Bedrock AgentCore Runtime, anywhere Python runs) so a team or CI pipeline points at one endpoint instead of installing locally.

This is *not* the full multi-user web platform — that's [the EKS deploy](../README.md#deploy-full-platform-on-eks). Self-hosting is just `eval-mcp serve` running somewhere reachable.

## What you get

- One HTTP MCP endpoint your IDE / CI / agent connects to
- Same tools as the local install (`analyze_agent_path`, `run_evaluation`, `list_evaluations`, etc.)
- Optional S3 replication so state survives container restarts and is shared across users

## Path 1: Plain Python on a VM

Spin up an EC2 instance (or any VM with Python 3.11+) and:

```bash
git clone https://github.com/awslabs/llm-evaluation-system.git
cd llm-evaluation-system
pip install -e .

# Optional — enable S3 replication for durability and team sharing
eval-mcp config set bucket my-team-evals

eval-mcp serve --host 0.0.0.0 --port 8002
```

Done. Point Claude Code or any MCP client at `http://<vm-host>:8002/mcp`.

AWS credentials come from the instance role (or `AWS_PROFILE` env var). Bedrock access is required.

## Path 2: Container

A `Dockerfile` is included at the repo root.

```bash
docker build -t eval-mcp:latest .
docker run --rm -p 8002:8002 \
    -e AWS_REGION=us-west-2 \
    -v eval-mcp-data:/data \
    eval-mcp:latest
```

The volume at `/data` (`EVAL_MCP_HOME`) holds datasets, judges, configs, eval logs, and PDF reports. Mount EBS, EFS, or any persistent volume in production.

If you'd rather treat the container as fully ephemeral, skip the volume and configure S3 replication — every write is mirrored to S3 within seconds:

```bash
docker run --rm -p 8002:8002 \
    -e AWS_REGION=us-west-2 \
    -e EVAL_MCP_BUCKET=my-team-evals \
    eval-mcp:latest
```

## Path 3: Kubernetes / EKS / AgentCore

The container in Path 2 is the unit of deployment. The shape is standard:

- **Workload**: `Deployment` (or AgentCore Runtime job) running the image
- **Service**: `ClusterIP` on port 8002, fronted by an `Ingress` / ALB / API Gateway with TLS
- **Storage**: PVC backed by EFS at `/data` *or* S3 replication via `EVAL_MCP_BUCKET`
- **Identity**: IRSA / pod identity / instance role with Bedrock + S3 access
- **Auth**: do this at the ingress (OIDC, mTLS, signed headers). `eval-mcp serve` does not authenticate callers itself.

A minimal Deployment looks like:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: eval-mcp
spec:
  replicas: 1
  selector:
    matchLabels: {app: eval-mcp}
  template:
    metadata:
      labels: {app: eval-mcp}
    spec:
      serviceAccountName: eval-mcp  # bound via IRSA to a role with bedrock + s3
      containers:
      - name: eval-mcp
        image: <your-registry>/eval-mcp:latest
        ports: [{containerPort: 8002}]
        env:
        - {name: AWS_REGION, value: us-west-2}
        - {name: EVAL_MCP_BUCKET, value: my-team-evals}
        volumeMounts:
        - {name: data, mountPath: /data}
      volumes:
      - name: data
        persistentVolumeClaim: {claimName: eval-mcp-data}
```

For AgentCore Runtime, package the same image and use AgentCore's deployment flow — the runtime contract is "serve HTTP on a port, accept MCP requests."

## Operational notes

- **Storage**: `/data` (`EVAL_MCP_HOME`) is the source of truth. Either persist it, or rely on S3 replication and treat `/data` as cache. Don't do both half-way.
- **Bedrock**: needs network egress to `bedrock-runtime.<region>.amazonaws.com` and `bedrock.<region>.amazonaws.com`. AWS PrivateLink works.
- **Auth**: zero auth in the binary. Always front it with an authenticating proxy when reachable beyond your VPC.
- **Concurrency**: replicas > 1 is fine if all replicas share `/data` (EFS) or rely on S3 replication. Don't run multiple replicas with separate local disks.
- **Logs**: stdout (the process logs) and `/data/users/{user}/logs/*.eval` (eval results). The latter is what `eval-mcp view` reads.

## Connecting Claude Code to a remote server

Point the IDE at the URL instead of spawning the binary locally. Example `.claude/settings.json`:

```json
{
  "mcpServers": {
    "eval": {
      "url": "https://eval-mcp.example.com/mcp"
    }
  }
}
```

Authentication is whatever your ingress requires (bearer token in `Authorization` header, etc. — set via your IDE's MCP auth config).

## When *not* to self-host

If you want chat history, multi-tenant auth, per-user isolation, and a polished web UI, deploy the full platform via `./deploy.sh` instead — see [the main README](../README.md#deploy-full-platform-on-eks). Self-hosting `eval-mcp` gives you the eval engine and the local viewer, nothing more.
