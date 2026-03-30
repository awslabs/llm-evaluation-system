#!/usr/bin/env bash
set -euo pipefail
#------------------------------------------------------------------------------
# deploy action — runs inside CodeBuild
# Expects env: ACCOUNT_ID, AWS_REGION, STATE_BUCKET, REGION_SUFFIX,
#              CALLER_ROLE_ARN (optional), CODEBUILD_SRC_DIR
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
# Phase 1: Ensure remote state bucket exists
#------------------------------------------------------------------------------

echo ""
echo "=== Phase 1: Ensuring remote state bucket exists ==="

if aws s3api head-bucket --bucket "$STATE_BUCKET" 2>/dev/null; then
  echo "  State bucket exists: $STATE_BUCKET"
else
  echo "  Creating state bucket: $STATE_BUCKET"
  if [ "$AWS_REGION" = "us-east-1" ]; then
    aws s3api create-bucket --bucket "$STATE_BUCKET" --region "$AWS_REGION"
  else
    aws s3api create-bucket --bucket "$STATE_BUCKET" --region "$AWS_REGION" \
      --create-bucket-configuration LocationConstraint="$AWS_REGION"
  fi
  aws s3api put-bucket-versioning --bucket "$STATE_BUCKET" \
    --versioning-configuration Status=Enabled
  aws s3api put-public-access-block --bucket "$STATE_BUCKET" \
    --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
  echo "  State bucket created with versioning enabled."
fi

#------------------------------------------------------------------------------
# Phase 2: Terraform configuration
#------------------------------------------------------------------------------

echo ""
echo "=== Phase 2: Terraform configuration ==="

# Create terraform.tfvars for data layer
cat > "$DATA_DIR/terraform.tfvars" <<EOF
region = "$AWS_REGION"
EOF
echo "  Created $DATA_DIR/terraform.tfvars"

# Create terraform.tfvars for platform layer
cp "$DATA_DIR/terraform.tfvars" "$PLATFORM_DIR/terraform.tfvars"
echo "  Created $PLATFORM_DIR/terraform.tfvars"

#------------------------------------------------------------------------------
# Phase 3: Terraform — Data layer (VPC, RDS, S3)
#------------------------------------------------------------------------------

echo ""
echo "=== Phase 3: Deploying data layer (VPC, RDS, S3 buckets) ==="

cd "$DATA_DIR"
terraform init -upgrade "${TF_BACKEND_ARGS[@]}"
tf_with_retry apply -auto-approve

# Read data-layer outputs
VPC_ID="$(terraform output -raw vpc_id)"
VPC_CIDR_BLOCK="$(terraform output -raw vpc_cidr_block)"
PRIVATE_SUBNETS="$(terraform output -json private_subnets)"
PUBLIC_SUBNETS="$(terraform output -json public_subnets)"
INTRA_SUBNETS="$(terraform output -json intra_subnets)"
RDS_ENDPOINT="$(terraform output -raw rds_endpoint)"
RDS_SECRET_ARN="$(terraform output -raw rds_secret_arn)"
RDS_SECURITY_GROUP_ID="$(terraform output -raw rds_security_group_id)"
RDS_RESOURCE_ID="$(terraform output -raw rds_resource_id)"
DOCUMENTS_BUCKET="$(terraform output -raw documents_bucket)"
DOCUMENTS_BUCKET_ARN="$(terraform output -raw documents_bucket_arn)"
BACKUP_BUCKET="$(terraform output -raw backup_bucket)"
BACKUP_BUCKET_ARN="$(terraform output -raw backup_bucket_arn)"

echo ""
echo "  Data layer deployed."
echo "  VPC: $VPC_ID"
echo "  RDS: $RDS_ENDPOINT"

#------------------------------------------------------------------------------
# Phase 4: Terraform — Platform layer (EKS, CloudFront, ALB, etc.)
#------------------------------------------------------------------------------

echo ""
echo "=== Phase 4: Deploying platform layer (EKS, CloudFront, ALB, Cognito, etc.) ==="

cd "$PLATFORM_DIR"

# Build the platform -var args
PLATFORM_VARS=(
  -var="vpc_id=$VPC_ID"
  -var="vpc_cidr_block=$VPC_CIDR_BLOCK"
  -var="private_subnets=$PRIVATE_SUBNETS"
  -var="public_subnets=$PUBLIC_SUBNETS"
  -var="intra_subnets=$INTRA_SUBNETS"
  -var="rds_endpoint=$RDS_ENDPOINT"
  -var="rds_secret_arn=$RDS_SECRET_ARN"
  -var="rds_security_group_id=$RDS_SECURITY_GROUP_ID"
  -var="rds_resource_id=$RDS_RESOURCE_ID"
  -var="documents_bucket=$DOCUMENTS_BUCKET"
  -var="documents_bucket_arn=$DOCUMENTS_BUCKET_ARN"
  -var="backup_bucket=$BACKUP_BUCKET"
  -var="backup_bucket_arn=$BACKUP_BUCKET_ARN"
)

# Pass caller's IAM role ARN so they get EKS admin access too
if [ -n "${CALLER_ROLE_ARN:-}" ]; then
  PLATFORM_VARS+=(-var="cluster_admin_role_arns=[\"$CALLER_ROLE_ARN\"]")
fi

terraform init -upgrade "${TF_BACKEND_ARGS[@]}"
tf_with_retry apply -auto-approve "${PLATFORM_VARS[@]}"

echo ""
echo "  Platform layer deployed."

#------------------------------------------------------------------------------
# Phase 5: Configure kubectl
#------------------------------------------------------------------------------

echo ""
echo "=== Phase 5: Configuring kubectl ==="

KUBECONFIG_CMD="$(terraform output -raw update_kubeconfig)"
eval "$KUBECONFIG_CMD"

echo "  Verifying cluster access..."
kubectl get nodes

echo "  Waiting for nodes to be Ready..."
kubectl wait --for=condition=Ready nodes --all --timeout=15m

echo "  Waiting for EKS add-ons to become ACTIVE..."
for addon in vpc-cni coredns kube-proxy aws-ebs-csi-driver; do
  for i in $(seq 1 60); do
    status=$(aws eks describe-addon --cluster-name eval-managed --addon-name "$addon" \
      --region "$AWS_REGION" --query 'addon.status' --output text 2>/dev/null) || status="NOT_FOUND"
    if [ "$status" = "ACTIVE" ]; then
      echo "    $addon: ACTIVE"
      break
    fi
    if [ "$i" -eq 60 ]; then
      echo "    WARNING: $addon status: $status (proceeding anyway)"
      break
    fi
    sleep 5
  done
done

echo "  Cluster ready."

#------------------------------------------------------------------------------
# Phase 6: Build and push Docker images
#------------------------------------------------------------------------------

echo ""
echo "=== Phase 6: Building container images ==="

IMAGE_REPO_NAME="eval-managed-${REGION_SUFFIX}/app"
REPOSITORY_URI="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${IMAGE_REPO_NAME}"

aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

echo "  Building backend image..."
docker build --platform linux/arm64 -f "$SRC/docker/backend.Dockerfile" -t "$REPOSITORY_URI:backend-latest" "$SRC"

echo "  Building frontend image..."
docker build --platform linux/arm64 -f "$SRC/docker/frontend.Dockerfile" -t "$REPOSITORY_URI:frontend-latest" "$SRC"

echo "  Pushing images to ECR..."
docker push "$REPOSITORY_URI:backend-latest"
docker push "$REPOSITORY_URI:frontend-latest"

echo "  Images pushed."

#------------------------------------------------------------------------------
# Phase 7: Helm deploy
#------------------------------------------------------------------------------

echo ""
echo "=== Phase 7: Deploying application via Helm ==="

cd "$SRC"
helm upgrade --install eval ./helm/eval \
  -n eval-managed \
  -f ./helm/eval/values-aws.yaml \
  --set "aws.region=$AWS_REGION" \
  --set-string "aws.accountId=$ACCOUNT_ID" \
  --set "projectName=eval-managed-${REGION_SUFFIX}" \
  --wait \
  --timeout 15m

echo "  Helm deploy complete."

# Force pods to pull the latest images
echo "  Restarting deployments to pick up new images..."
kubectl rollout restart deployment backend frontend -n eval-managed
kubectl rollout status deployment backend frontend -n eval-managed --timeout=30m

#------------------------------------------------------------------------------
# Phase 8: Write outputs to SSM Parameter Store
#------------------------------------------------------------------------------

echo ""
echo "=== Phase 8: Writing outputs to SSM ==="

cd "$PLATFORM_DIR"
APP_URL="$(terraform output -raw app_url)"
COGNITO_POOL_ID="$(terraform output -raw cognito_user_pool_id)"
EKS_CLUSTER_NAME="$(terraform output -raw eks_cluster_name)"

aws ssm put-parameter --name "/eval-managed/app-url" \
  --value "$APP_URL" --type String --overwrite --region "$AWS_REGION" > /dev/null
aws ssm put-parameter --name "/eval-managed/cognito-user-pool-id" \
  --value "$COGNITO_POOL_ID" --type String --overwrite --region "$AWS_REGION" > /dev/null
aws ssm put-parameter --name "/eval-managed/eks-cluster-name" \
  --value "$EKS_CLUSTER_NAME" --type String --overwrite --region "$AWS_REGION" > /dev/null

echo "  SSM parameters written."

#------------------------------------------------------------------------------
# Phase 9: Summary
#------------------------------------------------------------------------------

echo ""
echo "=============================================="
echo "  Deployment complete!"
echo "=============================================="
echo ""
echo "@@APP_URL=${APP_URL}@@"
echo "@@COGNITO_POOL_ID=${COGNITO_POOL_ID}@@"
echo "@@EKS_CLUSTER_NAME=${EKS_CLUSTER_NAME}@@"
echo ""
