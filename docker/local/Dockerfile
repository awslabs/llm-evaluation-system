# Local Deployment Dockerfile - Single container with all services
# Includes: PostgreSQL, FastAPI backend, Next.js frontend, 3 MCP servers, Inspect AI
#
# Build:  make build
# Run:    make run
# Dev:    make dev  (hot reload via volume mounts)

# ============================================================
# Stage 1: Build frontend (Next.js standalone)
# ============================================================
FROM public.ecr.aws/docker/library/node:22-alpine AS frontend-deps
WORKDIR /app
COPY frontend/package*.json ./
RUN npm ci

FROM public.ecr.aws/docker/library/node:22-alpine AS frontend-builder
WORKDIR /app
COPY --from=frontend-deps /app/node_modules ./node_modules
COPY frontend/ .
ENV BACKEND_URL=http://127.0.0.1:8080
ENV NEXT_TELEMETRY_DISABLED=1
ENV NEXT_PUBLIC_SHOW_CHAT=true
RUN npm run build

# ============================================================
# Stage 2: Runtime - all services in one container
# ============================================================
FROM public.ecr.aws/docker/library/node:22-alpine AS runtime

# System dependencies: Python, PostgreSQL, nginx, tini, bash
RUN apk add --no-cache python3 py3-pip postgresql nginx tini bash

# Install uv for fast Python package management
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Python dependencies (separate from source for caching)
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --extra dev --extra backend --extra k8s-sandbox --extra providers
ENV PATH="/app/.venv/bin:$PATH"

# Copy backend source
COPY backend/ ./backend/

# Copy local entrypoint (ASGI auth wrapper)
COPY local/ ./local/

# Copy frontend standalone build
COPY --from=frontend-builder /app/.next/standalone ./frontend-standalone/
COPY --from=frontend-builder /app/.next/static ./frontend-standalone/.next/static

# Copy nginx config and startup script
COPY docker/local/nginx.conf /etc/nginx/nginx.conf
COPY docker/local/start.sh /app/start.sh
RUN chmod +x /app/start.sh

# Create data directories owned by node user
RUN mkdir -p /data/users /data/pgdata /app/backend/logs \
    /var/lib/nginx/logs /var/lib/nginx/tmp \
    /app/frontend-src/node_modules \
    && chown -R node:node /data /app /var/lib/nginx /var/log/nginx

# -- Environment --
ENV PYTHONPATH=/app

ENV POSTGRES_HOST=127.0.0.1
ENV POSTGRES_PORT=5432
ENV POSTGRES_DB=evaldb
ENV POSTGRES_USER=node
ENV POSTGRES_PASSWORD=localdev

ENV SYNTHETIC_EVAL_MCP_URL=http://127.0.0.1:8002/mcp
ENV PROVIDERS_MCP_URL=http://127.0.0.1:8004/mcp
ENV DATASET_MCP_URL=http://127.0.0.1:8005/mcp

ENV USER_STORAGE_BASE=/data/users
ENV APP_URL=http://localhost:4001

USER node

EXPOSE 4001

ENTRYPOINT ["/sbin/tini", "--"]
CMD ["/app/start.sh"]
