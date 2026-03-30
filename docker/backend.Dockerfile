# Backend Dockerfile - FastAPI + Python MCP servers
FROM public.ecr.aws/docker/library/node:22-alpine

# Install Python, system dependencies, and tini (proper PID 1 signal handling)
RUN apk add --no-cache python3 py3-pip git tini

# Note: node:22-alpine already has 'node' user with uid 1000
# We'll use that user to match EFS permissions

# Copy and build promptfoo from local source with VITE_IS_HOSTED=1
# This makes the viewer use REST API instead of WebSocket for fetching latest eval
COPY promptfoo/ /tmp/promptfoo/
COPY backend/core/bedrock_pricing.json /tmp/backend/core/bedrock_pricing.json
WORKDIR /tmp/promptfoo
ENV VITE_IS_HOSTED=1
ENV PROMPTFOO_DISABLE_TELEMETRY=true
ENV NODE_OPTIONS="--max-old-space-size=8192"
RUN npm ci && npm run build && npm install -g .
WORKDIR /

# Install uv for reproducible Python builds
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Copy dependency files first (for layer caching)
COPY pyproject.toml uv.lock ./

# Install dependencies with locked versions
RUN uv sync --locked
ENV PATH="/app/.venv/bin:$PATH"

# Copy source code
COPY backend/ ./backend/

# Create directories for user data and logs, owned by node user (uid 1000)
RUN mkdir -p /data/users /app/backend/logs && chown -R node:node /data/users /app/backend/logs

ENV PYTHONPATH=/app
ENV USER_STORAGE_BASE=/data/users
ENV PROMPTFOO_DISABLE_UPDATE=true

USER node

EXPOSE 8080

# tini as entrypoint ensures child processes receive SIGTERM properly
# (PID 1 in containers ignores signals without an explicit handler)
ENTRYPOINT ["/sbin/tini", "--"]

CMD ["uvicorn", "backend.api.main:app", "--host", "0.0.0.0", "--port", "8080"]
