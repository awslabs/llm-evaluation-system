# Backend Dockerfile - FastAPI + Python MCP servers + Inspect AI
FROM public.ecr.aws/docker/library/python:3.12-slim

# Install system dependencies: tini (PID 1 signal handling)
RUN apt-get update && apt-get install -y --no-install-recommends tini \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Install Helm CLI (required by inspect-k8s-sandbox)
COPY --from=alpine/helm:3.17.3 /usr/bin/helm /usr/local/bin/helm

# Install kubectl (for agent pod management and code extraction)
COPY --from=bitnami/kubectl:latest /opt/bitnami/kubectl/bin/kubectl /usr/local/bin/kubectl

# Install uv for reproducible Python builds
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Copy dependency files first (for layer caching)
COPY pyproject.toml uv.lock ./

# Install dependencies with locked versions.
# The EKS web app needs Postgres (`backend`), the k8s container-agent path
# (`k8s-sandbox`), and non-Bedrock model SDKs (`providers`).
RUN uv sync --locked --extra backend --extra k8s-sandbox --extra providers
ENV PATH="/app/.venv/bin:$PATH"

# Copy source code
COPY backend/ ./backend/
COPY eval_mcp/ ./eval_mcp/

# Create non-root user (uid 1000) with home directory
RUN groupadd --gid 1000 appuser && useradd --uid 1000 --gid appuser --create-home appuser \
    && mkdir -p /data/users && chown -R appuser:appuser /data/users

ENV PYTHONPATH=/app
ENV USER_STORAGE_BASE=/data/users

USER appuser

EXPOSE 8080

# tini as entrypoint ensures child processes receive SIGTERM properly
# (PID 1 in containers ignores signals without an explicit handler)
ENTRYPOINT ["/usr/bin/tini", "--"]

CMD ["uvicorn", "backend.api.main:app", "--host", "0.0.0.0", "--port", "8080"]
