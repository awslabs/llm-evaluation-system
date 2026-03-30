#!/bin/bash
set -e

# ── Align promptfoo's Bedrock region with AWS_REGION ──
export AWS_BEDROCK_REGION=${AWS_BEDROCK_REGION:-$AWS_REGION}

# ── Set NODE_ENV based on mode ────────────────────────────
if [ "$DEV_MODE" = "true" ]; then
    export NODE_ENV=development
    # Volume mounts on macOS/Windows go through a VM — filesystem events don't
    # propagate. Poll so hot reload works on every OS and container runtime.
    export WATCHFILES_FORCE_POLLING=true
    export WATCHPACK_POLLING=true
    echo "=== Starting local deployment (dev mode) ==="
else
    export NODE_ENV=production
    echo "=== Starting local deployment ==="
fi

# ── Helper: wait for HTTP endpoint ────────────────────────
wait_for_url() {
    local name="$1" url="$2" timeout="${3:-60}"
    local elapsed=0
    echo "  Waiting for $name ..."
    while [ $elapsed -lt $timeout ]; do
        if wget -q -S -O /dev/null "$url" 2>&1 | grep -q "HTTP/"; then
            echo "  ✓ $name ready"
            return 0
        fi
        sleep 1
        elapsed=$((elapsed + 1))
    done
    echo "  ✗ $name failed to start within ${timeout}s"
    return 1
}

# ── Helper: start an MCP server ───────────────────────────
start_mcp() {
    local module="$1" watch_dir="$2"
    if [ "$DEV_MODE" = "true" ]; then
        watchfiles "python -m $module" "$watch_dir" &
    else
        python -m "$module" &
    fi
    MCP_PID=$!
}

# ── Trap: kill all children on exit ───────────────────────
cleanup() {
    trap - EXIT SIGTERM SIGINT
    echo ""
    echo "=== Shutting down ==="
    kill 0 2>/dev/null || true
    wait 2>/dev/null || true
    echo "=== Shutdown complete ==="
}
trap cleanup EXIT SIGTERM SIGINT

# ── Start PostgreSQL ──────────────────────────────────────
echo "Starting PostgreSQL..."

PGDATA=/data/pgdata

if [ ! -f "$PGDATA/PG_VERSION" ]; then
    echo "  Initializing database..."
    initdb -D "$PGDATA" --auth=trust --no-locale --encoding=UTF8 > /dev/null
    echo "host all all 127.0.0.1/32 trust" >> "$PGDATA/pg_hba.conf"
fi

pg_ctl start -D "$PGDATA" -o "-p 5432 -k /tmp" -l /data/pg.log -w
createdb -h 127.0.0.1 -p 5432 evaldb 2>/dev/null || true
echo "  ✓ PostgreSQL ready"

# ── Start MCP Servers (background) ───────────────────────
echo "Starting MCP servers..."

start_mcp backend.mcp_servers.synthetic.server_http /app/backend/mcp_servers/synthetic; SYNTHETIC_PID=$MCP_PID
start_mcp backend.mcp_servers.providers.server_http /app/backend/mcp_servers/providers; PROVIDERS_PID=$MCP_PID
start_mcp backend.mcp_servers.dataset.server_http   /app/backend/mcp_servers/dataset;   DATASET_PID=$MCP_PID

wait_for_url "Synthetic MCP" "http://127.0.0.1:8002/mcp" 60
wait_for_url "Providers MCP" "http://127.0.0.1:8004/mcp" 60
wait_for_url "Dataset MCP"   "http://127.0.0.1:8005/mcp" 60

# ── Start Backend ─────────────────────────────────────────
echo "Starting backend..."
if [ "$DEV_MODE" = "true" ]; then
    uvicorn local.entrypoint:app --host 0.0.0.0 --port 8080 --log-level info \
        --reload --reload-dir /app/backend --reload-dir /app/local &
else
    uvicorn local.entrypoint:app --host 0.0.0.0 --port 8080 --log-level info &
fi
BACKEND_PID=$!

wait_for_url "Backend" "http://127.0.0.1:8080/health" 30

# ── Start Frontend ────────────────────────────────────────
echo "Starting frontend..."
if [ "$DEV_MODE" = "true" ]; then
    cd /app/frontend-src
    if [ ! -d node_modules/next ]; then
        npm ci &>/dev/null
    fi
    HOSTNAME=127.0.0.1 PORT=3000 npx next dev &
    FRONTEND_PID=$!
    cd /app
else
    cd /app/frontend-standalone
    HOSTNAME=127.0.0.1 PORT=3000 node server.js &
    FRONTEND_PID=$!
    cd /app
fi

wait_for_url "Frontend" "http://127.0.0.1:3000" 30

# ── Start nginx (reverse proxy, exposed on :4001) ───────
echo "Starting nginx..."
nginx &
NGINX_PID=$!

wait_for_url "nginx" "http://127.0.0.1:4001" 10

echo ""
echo "============================================"
echo "  Local deployment ready!"
echo "  Open http://localhost:4001 in your browser"
if [ "$DEV_MODE" = "true" ]; then
echo "  Hot reload is active"
fi
echo "============================================"
echo ""

wait -n $SYNTHETIC_PID $PROVIDERS_PID $DATASET_PID $BACKEND_PID $FRONTEND_PID $NGINX_PID
EXIT_CODE=$?
echo "A process exited with code $EXIT_CODE, shutting down..."
exit $EXIT_CODE
