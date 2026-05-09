#!/bin/bash
# Run all example evaluations in parallel to validate the pipeline.
# Usage: AWS_PROFILE=andgg-Admin ./examples/run_all_tests.sh

set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
INSPECT="$ROOT/.venv/bin/inspect"
MODEL="bedrock/us.anthropic.claude-sonnet-4-6"

echo "=== Running all eval tests in parallel ==="
echo ""

# 1. Local agent (boto3 direct, with tools)
echo "[1/3] Local agent eval (boto3 + tools)..."
(cd "$ROOT" && $INSPECT eval examples/local_agent/task.py --model $MODEL --max-connections 2 --no-log-images --no-fail-on-error) &
PID1=$!

# 2. Strands multi-agent
echo "[2/3] Strands multi-agent eval..."
(cd /Users/andgg/.eval-mcp/users/local && $INSPECT eval configs/strands_test.py --model $MODEL --max-connections 1 --no-log-images --no-fail-on-error) &
PID2=$!

# 3. Non-agentic model comparison (uses existing dataset + jury scoring)
echo "[3/3] Model comparison eval (non-agentic)..."
(cd /Users/andgg/.eval-mcp/users/local && $INSPECT eval configs/cloud_test2.py --model $MODEL --max-connections 2 --no-log-images --no-fail-on-error) &
PID3=$!

echo ""
echo "Waiting for all evals to complete..."
echo ""

wait $PID1
STATUS1=$?
echo "[1/3] Local agent: $([ $STATUS1 -eq 0 ] && echo 'PASS' || echo 'FAIL')"

wait $PID2
STATUS2=$?
echo "[2/3] Strands agent: $([ $STATUS2 -eq 0 ] && echo 'PASS' || echo 'FAIL')"

wait $PID3
STATUS3=$?
echo "[3/3] Model comparison: $([ $STATUS3 -eq 0 ] && echo 'PASS' || echo 'FAIL')"

echo ""
echo "=== Done ==="
