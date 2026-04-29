#!/usr/bin/env bash
set -euo pipefail
#------------------------------------------------------------------------------
# destroy-data action — runs inside CodeBuild
# Destroys data layer (VPC, RDS, S3 buckets) — PERMANENT DATA LOSS
# Expects env: ACCOUNT_ID, AWS_REGION, STATE_BUCKET, CODEBUILD_SRC_DIR
#------------------------------------------------------------------------------

SRC="$CODEBUILD_SRC_DIR"
DATA_DIR="$SRC/infra/data"
TF_BACKEND_ARGS=(-backend-config="bucket=$STATE_BUCKET" -backend-config="region=$AWS_REGION")

# Retry wrapper for terraform commands — handles transient AWS API errors
tf_with_retry() {
  local max_attempts=3
  local attempt=1
  local wait_seconds=30

  while [ "$attempt" -le "$max_attempts" ]; do
    if [ "$attempt" -gt 1 ]; then
      echo ""
      echo "  Retry attempt $attempt/$max_attempts (waiting ${wait_seconds}s)..."
      sleep "$wait_seconds"
      wait_seconds=$((wait_seconds * 2))
    fi

    if terraform "$@"; then
      return 0
    fi

    if [ "$attempt" -eq "$max_attempts" ]; then
      echo "  ERROR: terraform $1 failed after $max_attempts attempts."
      return 1
    fi

    echo "  WARNING: terraform $1 failed, will retry..."
    attempt=$((attempt + 1))
  done
}

#------------------------------------------------------------------------------
# Phase 1: Destroy data layer
#------------------------------------------------------------------------------

echo ""
echo "=== Phase 1: Destroying data layer (VPC, RDS, S3 buckets) ==="

# Create terraform.tfvars with the correct region
cat > "$DATA_DIR/terraform.tfvars" <<EOF
region = "$AWS_REGION"
EOF

cd "$DATA_DIR"
terraform init -upgrade "${TF_BACKEND_ARGS[@]}"
tf_with_retry destroy -auto-approve

echo "  Data layer destroyed."

#------------------------------------------------------------------------------
# Phase 2: Summary
#------------------------------------------------------------------------------

echo ""
echo "=============================================="
echo "  Data layer destroyed"
echo "=============================================="
echo ""
echo "  All infrastructure has been removed:"
echo "    - VPC and subnets"
echo "    - RDS PostgreSQL database"
echo "    - S3 documents bucket"
echo "    - S3 data bucket"
echo ""
echo "  The following bootstrap resources still exist:"
echo "    - S3 state bucket: ${STATE_BUCKET}"
echo "    - CodeBuild project: eval-managed-deploy-runner"
echo "    - IAM role: eval-managed-deploy-runner"
echo ""
echo "  To remove bootstrap resources:"
echo "    ./destroy.sh --cleanup-bootstrap"
echo ""
echo "@@DESTROY_DATA_COMPLETE=true@@"
echo ""
