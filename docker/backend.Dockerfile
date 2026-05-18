# Backend Dockerfile - FastAPI + Python MCP servers + Inspect AI
FROM public.ecr.aws/docker/library/python:3.12-slim

# Install system dependencies: tini (PID 1 signal handling), curl (for tool
# installs below — kept on PATH because the live container also uses it).
RUN apt-get update && apt-get install -y --no-install-recommends tini curl \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# kubectl + Helm — installed via direct download from canonical upstream
# CDNs with pinned versions + SHA256 verification. This replaces multi-stage
# `COPY --from=bitnami/kubectl:latest` / `alpine/helm:...` which (a) pulled
# from Docker Hub and tripped its anonymous-pull rate limit in CodeBuild,
# and (b) gave us an unpinned `:latest` with no checksum check.
#
# To bump: edit the version + paste the new SHA256 from
#   https://dl.k8s.io/release/v${KUBECTL_VERSION}/bin/linux/arm64/kubectl.sha256
#   https://get.helm.sh/helm-v${HELM_VERSION}-linux-arm64.tar.gz.sha256sum
ARG KUBECTL_VERSION=1.32.4
ARG KUBECTL_SHA256=c6f96d0468d6976224f5f0d81b65e1a63b47195022646be83e49d38389d572c2
ARG HELM_VERSION=3.17.3
ARG HELM_SHA256=7944e3defd386c76fd92d9e6fec5c2d65a323f6fadc19bfb5e704e3eee10348e

RUN set -eux; \
    curl -fsSL --retry 3 --retry-delay 5 \
      "https://dl.k8s.io/release/v${KUBECTL_VERSION}/bin/linux/arm64/kubectl" \
      -o /usr/local/bin/kubectl; \
    echo "${KUBECTL_SHA256}  /usr/local/bin/kubectl" | sha256sum -c -; \
    chmod +x /usr/local/bin/kubectl; \
    \
    curl -fsSL --retry 3 --retry-delay 5 \
      "https://get.helm.sh/helm-v${HELM_VERSION}-linux-arm64.tar.gz" \
      -o /tmp/helm.tar.gz; \
    echo "${HELM_SHA256}  /tmp/helm.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/helm.tar.gz -C /usr/local/bin --strip-components=1 linux-arm64/helm; \
    rm /tmp/helm.tar.gz

# Install uv for reproducible Python builds (GHCR, not rate-limited)
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
