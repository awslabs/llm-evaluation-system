#!/usr/bin/env bash
set -euo pipefail
#------------------------------------------------------------------------------
# destroy action — runs inside CodeBuild
# Destroys platform layer, preserves data layer (VPC, RDS, S3)
# Expects env: ACCOUNT_ID, AWS_REGION, STATE_BUCKET, REGION_SUFFIX, CODEBUILD_SRC_DIR
#------------------------------------------------------------------------------

SRC="$CODEBUILD_SRC_DIR"
DATA_DIR="$SRC/infra/data"
PLATFORM_DIR="$SRC/infra/platform"
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
# Phase 1: Create RDS snapshot
#------------------------------------------------------------------------------

echo ""
echo "=== Phase 1: Creating RDS snapshot ==="

# Create terraform.tfvars with the correct region
cat > "$DATA_DIR/terraform.tfvars" <<EOF
region = "$AWS_REGION"
EOF
cat > "$PLATFORM_DIR/terraform.tfvars" <<EOF
region = "$AWS_REGION"
EOF

cd "$DATA_DIR"
terraform init -upgrade "${TF_BACKEND_ARGS[@]}" > /dev/null

RDS_IDENTIFIER="$(terraform output -raw rds_identifier 2>&1)" || true
if [ -n "$RDS_IDENTIFIER" ] && [[ "$RDS_IDENTIFIER" != *"Warning"* ]] && [[ "$RDS_IDENTIFIER" != *"No outputs"* ]]; then
  SNAPSHOT_ID="${RDS_IDENTIFIER}-$(date +%Y%m%d-%H%M%S)"

  echo "  Creating snapshot: $SNAPSHOT_ID"
  aws rds create-db-snapshot \
    --db-instance-identifier "$RDS_IDENTIFIER" \
    --db-snapshot-identifier "$SNAPSHOT_ID" \
    --region "$AWS_REGION" \
    --query 'DBSnapshot.DBSnapshotIdentifier' \
    --output text

  echo "  Waiting for snapshot to become available..."
  aws rds wait db-snapshot-available \
    --db-snapshot-identifier "$SNAPSHOT_ID" \
    --region "$AWS_REGION"

  echo "  Snapshot ready: $SNAPSHOT_ID"
else
  echo "  WARNING: Could not read RDS identifier. Skipping snapshot."
  RDS_IDENTIFIER="unknown"
  SNAPSHOT_ID="none"
fi

#------------------------------------------------------------------------------
# Phase 2: Helm uninstall
#------------------------------------------------------------------------------

echo ""
echo "=== Phase 2: Helm uninstall ==="

CLUSTER_NAME="eval-managed"
if aws eks describe-cluster --name "$CLUSTER_NAME" --region "$AWS_REGION" &>/dev/null; then
  aws eks update-kubeconfig --name "$CLUSTER_NAME" --region "$AWS_REGION" 2>/dev/null || true
  helm uninstall eval -n eval-managed --no-hooks 2>/dev/null || true
  echo "  Helm release uninstalled."
else
  echo "  EKS cluster not found — skipping helm uninstall."
fi

#------------------------------------------------------------------------------
# Phase 3: Destroy platform layer
#------------------------------------------------------------------------------

echo ""
echo "=== Phase 3: Destroying platform layer ==="

# Read data-layer outputs to pass as vars
cd "$DATA_DIR"
VPC_ID="$(terraform output -raw vpc_id 2>/dev/null)" || true
if [ -z "$VPC_ID" ] || [[ "$VPC_ID" == *"Warning"* ]] || [[ "$VPC_ID" == *"not found"* ]] || [[ "$VPC_ID" == *"No outputs"* ]]; then
  echo "  WARNING: Data layer has no outputs — skipping platform destroy."
  echo "  Run 'destroy-data' to clean up the data layer directly."
  VPC_ID="unknown"
  DOCUMENTS_BUCKET="unknown"
  DATA_BUCKET="unknown"
else
VPC_CIDR_BLOCK="$(terraform output -raw vpc_cidr_block)"
PRIVATE_SUBNETS="$(terraform output -json private_subnets)"
PUBLIC_SUBNETS="$(terraform output -json public_subnets)"
INTRA_SUBNETS="$(terraform output -json intra_subnets)"
RDS_ENDPOINT="$(terraform output -raw rds_endpoint)"
RDS_SECRET_ARN="$(terraform output -raw rds_secret_arn)"
RDS_SECURITY_GROUP_ID="$(terraform output -raw rds_security_group_id)"
RDS_RESOURCE_ID="$(terraform output -raw rds_resource_id)"
DOCUMENTS_BUCKET="$(terraform output -raw documents_bucket 2>/dev/null || echo unknown)"
DOCUMENTS_BUCKET_ARN="$(terraform output -raw documents_bucket_arn 2>/dev/null || echo unknown)"
DATA_BUCKET="$(terraform output -raw data_bucket 2>/dev/null || terraform output -raw backup_bucket 2>/dev/null || echo unknown)"
DATA_BUCKET_ARN="$(terraform output -raw data_bucket_arn 2>/dev/null || terraform output -raw backup_bucket_arn 2>/dev/null || echo unknown)"
SPA_BUCKET="$(terraform output -raw spa_bucket 2>/dev/null || echo unknown)"
SPA_BUCKET_ARN="$(terraform output -raw spa_bucket_arn 2>/dev/null || echo unknown)"
SPA_BUCKET_REGIONAL_DOMAIN="$(terraform output -raw spa_bucket_regional_domain_name 2>/dev/null || echo unknown)"

cd "$PLATFORM_DIR"
terraform init -upgrade "${TF_BACKEND_ARGS[@]}"
tf_with_retry destroy -auto-approve \
  -var="vpc_id=$VPC_ID" \
  -var="vpc_cidr_block=$VPC_CIDR_BLOCK" \
  -var="private_subnets=$PRIVATE_SUBNETS" \
  -var="public_subnets=$PUBLIC_SUBNETS" \
  -var="intra_subnets=$INTRA_SUBNETS" \
  -var="rds_endpoint=$RDS_ENDPOINT" \
  -var="rds_secret_arn=$RDS_SECRET_ARN" \
  -var="rds_security_group_id=$RDS_SECURITY_GROUP_ID" \
  -var="rds_resource_id=$RDS_RESOURCE_ID" \
  -var="documents_bucket=$DOCUMENTS_BUCKET" \
  -var="documents_bucket_arn=$DOCUMENTS_BUCKET_ARN" \
  -var="data_bucket=$DATA_BUCKET" \
  -var="data_bucket_arn=$DATA_BUCKET_ARN" \
  -var="spa_bucket=$SPA_BUCKET" \
  -var="spa_bucket_arn=$SPA_BUCKET_ARN" \
  -var="spa_bucket_regional_domain_name=$SPA_BUCKET_REGIONAL_DOMAIN"

echo "  Platform layer destroyed."
fi

#------------------------------------------------------------------------------
# Phase 4: Clean up SSM parameters
#------------------------------------------------------------------------------

echo ""
echo "=== Phase 4: Cleaning up SSM parameters ==="

for param in /eval-managed/app-url /eval-managed/cognito-user-pool-id /eval-managed/eks-cluster-name; do
  aws ssm delete-parameter --name "$param" --region "$AWS_REGION" 2>/dev/null || true
done
echo "  SSM parameters deleted."

#------------------------------------------------------------------------------
# Phase 5: Preservation summary
#------------------------------------------------------------------------------

echo ""
echo "=============================================="
echo "  Teardown complete — preserved resources"
echo "=============================================="
echo ""
echo "  The following resources still exist and incur costs:"
echo ""
echo "  RDS PostgreSQL:"
echo "    Identifier: $RDS_IDENTIFIER"
echo "    Snapshot:    $SNAPSHOT_ID"
echo ""
echo "  S3 Documents Bucket: $DOCUMENTS_BUCKET"
echo "  S3 Data Bucket:      $DATA_BUCKET"
echo "  VPC:                 $VPC_ID"
echo ""
echo "@@DESTROY_COMPLETE=true@@"
echo "@@RDS_IDENTIFIER=${RDS_IDENTIFIER}@@"
echo "@@SNAPSHOT_ID=${SNAPSHOT_ID}@@"
echo "@@DOCUMENTS_BUCKET=${DOCUMENTS_BUCKET}@@"
echo "@@DATA_BUCKET=${DATA_BUCKET}@@"
echo "@@VPC_ID=${VPC_ID}@@"
echo ""
