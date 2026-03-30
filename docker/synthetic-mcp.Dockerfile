# Synthetic Eval MCP Server
FROM public.ecr.aws/docker/library/python:3.12-slim

# Install system dependencies and Node.js (for promptfoo CLI)
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Install promptfoo globally (for run_evaluation tool)
RUN npm install -g promptfoo@0.119.0

WORKDIR /app

# Install uv for locked installs
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy source and install dependencies
COPY pyproject.toml uv.lock ./
COPY backend/ ./backend/
RUN uv sync --locked
ENV PATH="/app/.venv/bin:$PATH"

# Create non-root user (uid 1000 to match k8s securityContext) and data directories
RUN groupadd --gid 1000 appuser && useradd --uid 1000 --gid appuser appuser \
    && mkdir -p /data/users && chown -R appuser:appuser /data/users

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
ENV USER_STORAGE_BASE=/data/users

USER appuser

EXPOSE 8002

CMD ["python", "-m", "backend.mcp_servers.synthetic.server_http"]
