FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# uv = fast deterministic resolver (pip's backtracking is too slow on this graph)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY eval_mcp/ ./eval_mcp/
COPY backend/ ./backend/

# Slim runtime: only the core deps. Add --extra providers if you eval
# non-Bedrock models, --extra k8s-sandbox for containerized agent evals.
RUN uv sync --locked --no-dev
ENV PATH="/app/.venv/bin:$PATH"

# Persistent state (datasets, judges, configs, logs, reports)
ENV EVAL_MCP_HOME=/data
RUN mkdir -p /data

EXPOSE 8002

# Bedrock credentials come from the platform (instance role / IRSA / env).
# S3 replication: set EVAL_MCP_BUCKET, or `eval-mcp config set bucket <name>`
# inside a writable /data volume, or pre-write /data/config.json before launch.
CMD ["eval-mcp", "serve", "--host", "0.0.0.0", "--port", "8002"]
