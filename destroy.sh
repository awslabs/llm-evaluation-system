#!/usr/bin/env bash
set -euo pipefail

#------------------------------------------------------------------------------
# destroy.sh — Tear down the LLM Evaluation Platform via CodeBuild
#
# Usage: ./destroy.sh                   Destroy platform layer (preserve data)
#        ./destroy.sh --cleanup-bootstrap  Also remove CodeBuild project + IAM role
#
# Thin orchestrator that only needs AWS CLI. Triggers CodeBuild to run
# terraform destroy and helm uninstall.
#
# What gets DESTROYED: EKS, CloudFront, WAF, ALB, Cognito, CodeBuild (app),
#   ECR, Bedrock logging, K8s resources, source S3 bucket
#
# What gets PRESERVED: VPC, RDS PostgreSQL, S3 documents bucket, S3 backup bucket
#------------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PROJECT_NAME="eval-managed"
CODEBUILD_PROJECT="${PROJECT_NAME}-deploy-runner"
IAM_ROLE_NAME="${PROJECT_NAME}-deploy-runner"

CLEANUP_BOOTSTRAP=false
for arg in "$@"; do
  case "$arg" in
    --cleanup-bootstrap) CLEANUP_BOOTSTRAP=true ;;
  esac
done

#------------------------------------------------------------------------------
# Phase 1: Confirm destructive action
#------------------------------------------------------------------------------

echo "=============================================="
echo "  DESTROY — Platform Layer Teardown"
echo "=============================================="
echo ""
echo "  WILL BE DESTROYED:"
echo "    - EKS cluster and node groups"
echo "    - CloudFront distribution and WAF"
echo "    - Application Load Balancer"
echo "    - Cognito User Pool"
echo "    - CodeBuild project (app) and source S3 bucket"
echo "    - ECR repository (container images)"
echo "    - Bedrock logging configuration"
echo "    - All Kubernetes resources"
echo ""
echo "  WILL BE PRESERVED:"
echo "    - VPC and subnets"
echo "    - RDS PostgreSQL database"
echo "    - S3 documents bucket (user uploads)"
echo "    - S3 backup bucket (SQLite backups)"
echo ""
printf "Type 'destroy' to confirm: "
read -r CONFIRM
if [ "$CONFIRM" != "destroy" ]; then
  echo "Aborted."
  exit 0
fi

#------------------------------------------------------------------------------
# Phase 2: Validate AWS credentials
#------------------------------------------------------------------------------

echo ""
echo "=== Phase 2: Validating AWS credentials ==="

if [ -z "${AWS_PROFILE:-}" ] && [ -z "${AWS_ACCESS_KEY_ID:-}" ]; then
  echo "  No AWS_PROFILE set. Available profiles:"
  aws configure list-profiles 2>/dev/null | sed 's/^/    /'
  echo ""
  printf "Enter AWS profile name: "
  read -r PROFILE_INPUT
  if [ -z "$PROFILE_INPUT" ]; then
    echo "ERROR: No profile specified."
    exit 1
  fi
  export AWS_PROFILE="$PROFILE_INPUT"
  echo "  Using AWS_PROFILE=$AWS_PROFILE"
fi

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
IDENTITY_ARN="$(aws sts get-caller-identity --query Arn --output text)"

if [ -z "${AWS_REGION:-}" ]; then
  AWS_REGION="$(aws configure get region 2>/dev/null || true)"
  if [ -z "$AWS_REGION" ]; then
    echo "ERROR: AWS_REGION not set and no default region configured."
    exit 1
  fi
  export AWS_REGION
fi

REGION_SUFFIX="$(echo "$AWS_REGION" | tr -d '-')"
STATE_BUCKET="eval-managed-tfstate-${ACCOUNT_ID}-${REGION_SUFFIX}"

echo "  Account:  $ACCOUNT_ID"
echo "  Identity: $IDENTITY_ARN"
echo "  Region:   $AWS_REGION"

#------------------------------------------------------------------------------
# Phase 3: Verify deploy-runner exists
#------------------------------------------------------------------------------

echo ""
echo "=== Phase 3: Checking deploy-runner ==="

if ! aws codebuild batch-get-projects --names "$CODEBUILD_PROJECT" --region "$AWS_REGION" \
    --query 'projects[0].name' --output text 2>/dev/null | grep -q "$CODEBUILD_PROJECT"; then
  echo "ERROR: CodeBuild project '$CODEBUILD_PROJECT' not found."
  echo "  Nothing to destroy — the deploy-runner has not been bootstrapped."
  exit 1
fi
echo "  CodeBuild project found: $CODEBUILD_PROJECT"

#------------------------------------------------------------------------------
# Phase 4: Zip and upload source to S3
#------------------------------------------------------------------------------

echo ""
echo "=== Phase 4: Uploading source to S3 ==="

cd "$SCRIPT_DIR"
TMPZIP="$(mktemp /tmp/source-XXXXXX).zip"
zip -r "$TMPZIP" . \
  -x "*.git*" \
  -x "*/node_modules/*" \
  -x "*/.next/*" \
  -x "*.terraform*" \
  -x "*/.venv/*" \
  -x "*.tfstate*" \
  -x "*/.tools/*" \
  -x "*/__pycache__/*" \
  -x "*.pyc" \
  -x "*.tfvars" \
  > /dev/null

aws s3 cp "$TMPZIP" "s3://$STATE_BUCKET/deploy-source/source.zip" --region "$AWS_REGION"
rm -f "$TMPZIP"
echo "  Source uploaded."

#------------------------------------------------------------------------------
# Phase 5: Start destroy build
#------------------------------------------------------------------------------

echo ""
echo "=== Phase 5: Starting destroy build ==="

BUILD_ID="$(aws codebuild start-build \
  --project-name "$CODEBUILD_PROJECT" \
  --region "$AWS_REGION" \
  --environment-variables-override '[
    {"name":"ACTION","value":"destroy","type":"PLAINTEXT"},
    {"name":"AWS_REGION","value":"'"$AWS_REGION"'","type":"PLAINTEXT"}
  ]' \
  --query 'build.id' \
  --output text)"

echo "  Build ID: $BUILD_ID"

BUILD_UUID="${BUILD_ID#*:}"
echo "  Console:  https://${AWS_REGION}.console.aws.amazon.com/codesuite/codebuild/${ACCOUNT_ID}/projects/${CODEBUILD_PROJECT}/build/${CODEBUILD_PROJECT}%3A${BUILD_UUID}"
echo ""
echo "  Waiting for build to complete..."

#------------------------------------------------------------------------------
# Phase 6: Poll build status and tail logs
#------------------------------------------------------------------------------

poll_build() {
  local build_id="$1"
  local success_msg="${2:-Build SUCCEEDED.}"
  local log_group="" log_stream="" log_token=""

  while true; do
    local build_status
    build_status="$(aws codebuild batch-get-builds \
      --ids "$build_id" \
      --region "$AWS_REGION" \
      --query 'builds[0].buildStatus' \
      --output text)"

    if [ -z "$log_group" ]; then
      log_group="$(aws codebuild batch-get-builds \
        --ids "$build_id" \
        --region "$AWS_REGION" \
        --query 'builds[0].logs.groupName' \
        --output text 2>/dev/null || echo "")"
      log_stream="$(aws codebuild batch-get-builds \
        --ids "$build_id" \
        --region "$AWS_REGION" \
        --query 'builds[0].logs.streamName' \
        --output text 2>/dev/null || echo "")"
      [ "$log_group" = "None" ] && log_group=""
      [ "$log_stream" = "None" ] && log_stream=""
    fi

    if [ -n "$log_group" ] && [ -n "$log_stream" ]; then
      local log_args=(
        --log-group-name "$log_group"
        --log-stream-name "$log_stream"
        --start-from-head
        --region "$AWS_REGION"
      )
      if [ -n "$log_token" ]; then
        log_args+=(--next-token "$log_token")
      fi

      local log_output
      log_output="$(aws logs get-log-events "${log_args[@]}" --output json 2>/dev/null || echo '{}')"

      local new_token
      new_token="$(echo "$log_output" | python3 -c "import sys,json; print(json.load(sys.stdin).get('nextForwardToken',''))" 2>/dev/null || echo "")"
      if [ -n "$new_token" ] && [ "$new_token" != "$log_token" ]; then
        echo "$log_output" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for event in data.get('events', []):
    print('  [build] ' + event.get('message', '').rstrip())
" 2>/dev/null || true
        log_token="$new_token"
      fi
    fi

    case "$build_status" in
      SUCCEEDED)
        echo ""
        echo "  $success_msg"
        return 0
        ;;
      FAILED|FAULT|STOPPED|TIMED_OUT)
        echo ""
        echo "ERROR: Build $build_status."
        echo "  Check logs: aws codebuild batch-get-builds --ids $build_id --region $AWS_REGION"
        return 1
        ;;
      *)
        sleep 15
        ;;
    esac
  done
}

poll_build "$BUILD_ID" "Destroy build SUCCEEDED." || exit 1

#------------------------------------------------------------------------------
# Phase 7: Preservation summary
#------------------------------------------------------------------------------

echo ""
echo "=============================================="
echo "  Teardown complete — preserved resources"
echo "=============================================="
echo ""
echo "  The following resources still exist and incur costs:"
echo ""
echo "  - VPC and subnets"
echo "  - RDS PostgreSQL database"
echo "  - S3 documents bucket (user uploads)"
echo "  - S3 backup bucket (SQLite backups)"
echo ""
echo "  To see specific resource IDs, check the build logs above."
echo ""

#------------------------------------------------------------------------------
# Phase 8: Offer data layer destruction
#------------------------------------------------------------------------------

echo "  Also destroy data layer? This will PERMANENTLY DELETE:"
echo "    - RDS PostgreSQL database (all data)"
echo "    - S3 documents bucket (all uploads)"
echo "    - S3 backup bucket"
echo "    - VPC and subnets"
echo ""
printf "Type 'destroy-data' to confirm, or press Enter to skip: "
read -r CONFIRM_DATA

if [ "$CONFIRM_DATA" = "destroy-data" ]; then
  echo ""
  echo "=== Starting data layer destroy ==="

  BUILD_ID="$(aws codebuild start-build \
    --project-name "$CODEBUILD_PROJECT" \
    --region "$AWS_REGION" \
    --environment-variables-override '[
      {"name":"ACTION","value":"destroy-data","type":"PLAINTEXT"},
      {"name":"AWS_REGION","value":"'"$AWS_REGION"'","type":"PLAINTEXT"}
    ]' \
    --query 'build.id' \
    --output text)"

  echo "  Build ID: $BUILD_ID"
  echo "  Waiting for build to complete..."

  poll_build "$BUILD_ID" "Data layer destroy SUCCEEDED." || exit 1
else
  echo ""
  echo "  Skipped. To destroy data layer later, re-run: ./destroy.sh"
  echo "  Or manually: trigger CodeBuild with ACTION=destroy-data"
fi

#------------------------------------------------------------------------------
# Phase 9: Optional bootstrap cleanup
#------------------------------------------------------------------------------

if [ "$CLEANUP_BOOTSTRAP" = true ]; then
  echo ""
  echo "=== Cleaning up bootstrap resources ==="

  echo "  Deleting CodeBuild project: $CODEBUILD_PROJECT"
  aws codebuild delete-project --name "$CODEBUILD_PROJECT" --region "$AWS_REGION" 2>/dev/null || true

  echo "  Detaching policies from IAM role: $IAM_ROLE_NAME"
  aws iam detach-role-policy \
    --role-name "$IAM_ROLE_NAME" \
    --policy-arn "arn:aws:iam::aws:policy/AdministratorAccess" 2>/dev/null || true

  echo "  Deleting IAM role: $IAM_ROLE_NAME"
  aws iam delete-role --role-name "$IAM_ROLE_NAME" 2>/dev/null || true

  echo "  Bootstrap resources removed."
  echo ""
  echo "  Note: The S3 state bucket ($STATE_BUCKET) was NOT deleted."
  echo "  To delete it manually:"
  echo "    aws s3 rb s3://$STATE_BUCKET --force --region $AWS_REGION"
fi

echo ""
echo "  Done."
echo ""
