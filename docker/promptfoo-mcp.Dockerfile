# Promptfoo MCP Server (local fork with multi-tenancy)
FROM public.ecr.aws/docker/library/node:20-slim

# Install tini for proper signal handling, python3 for providers, procps for build
RUN apt-get update && apt-get install -y --no-install-recommends tini python3 procps && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy all source code first
COPY promptfoo/ ./
COPY backend/core/bedrock_pricing.json /backend/core/bedrock_pricing.json

# Install dependencies (both root and app workspace)
RUN npm ci
RUN cd src/app && npm ci

# Build the project (skip test type checking for production)
# The standard build runs: tsc --noEmit && tsdown && npm run build:app
# We skip tsc on tests by running tsdown and build:app directly
RUN ./node_modules/.bin/tsdown && npm run build:app

# Link globally so we can use npx promptfoo
RUN npm link

# Create non-root user (uid 1000 to match k8s securityContext) and data directory
RUN groupadd --gid 1000 appuser && useradd --uid 1000 --gid appuser appuser \
    && mkdir -p /data/shared && chown -R appuser:appuser /data/shared

ENV PROMPTFOO_CONFIG_DIR=/data/shared
ENV PROMPTFOO_CACHE_ENABLED=false

USER appuser

EXPOSE 8000

# Use tini as init to handle signals properly
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["npx", "promptfoo", "mcp", "--transport", "http", "--port", "8000"]
