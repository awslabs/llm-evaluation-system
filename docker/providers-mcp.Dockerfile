# Providers MCP Server (Bedrock model discovery)
FROM public.ecr.aws/docker/library/python:3.12-slim

WORKDIR /app

# Install uv for locked installs
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy source and install dependencies
COPY pyproject.toml uv.lock ./
COPY backend/ ./backend/
RUN uv sync --locked
ENV PATH="/app/.venv/bin:$PATH"

# Create non-root user (uid 1000 to match k8s securityContext)
RUN groupadd --gid 1000 appuser && useradd --uid 1000 --gid appuser appuser

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

USER appuser

EXPOSE 8004

CMD ["python", "-m", "backend.mcp_servers.providers.server_http"]
