#!/usr/bin/env bash
set -euo pipefail

#------------------------------------------------------------------------------
# deploy.sh — One-command deployment for the LLM Evaluation Platform
#
# Usage: ./deploy.sh
#
# Thin orchestrator that only needs AWS CLI. Bootstraps a CodeBuild project,
# uploads source, triggers the build, and polls logs. All heavy lifting
# (terraform, kubectl, helm, Docker builds) runs inside CodeBuild.
#
# Idempotent: safe to re-run at any point.
#------------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PROJECT_NAME="eval-managed"
CODEBUILD_PROJECT="${PROJECT_NAME}-deploy-runner"
IAM_ROLE_NAME="${PROJECT_NAME}-deploy-runner"
CODEBUILD_IMAGE="aws/codebuild/amazonlinux2-aarch64-standard:3.0"
CODEBUILD_COMPUTE="BUILD_GENERAL1_LARGE"

#------------------------------------------------------------------------------
# Phase 1: Prerequisites
#------------------------------------------------------------------------------

echo "=== Phase 1: Checking prerequisites ==="

if ! command -v aws &>/dev/null; then
  echo "ERROR: 'aws' CLI is required but not found."
  echo "Install AWS CLI: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
  exit 1
fi
echo "  aws CLI: OK"

#------------------------------------------------------------------------------
# Phase 2: AWS credentials validation
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
    echo "Set it with: export AWS_REGION=us-west-2"
    exit 1
  fi
  export AWS_REGION
fi

REGION_SUFFIX="$(echo "$AWS_REGION" | tr -d '-')"
STATE_BUCKET="eval-managed-tfstate-${ACCOUNT_ID}-${REGION_SUFFIX}"

echo ""
echo "  Account:  $ACCOUNT_ID"
echo "  Identity: $IDENTITY_ARN"
echo "  Region:   $AWS_REGION"
echo ""
printf "Proceed with deployment to this account? (y/N): "
read -r CONFIRM
if [ "$CONFIRM" != "y" ] && [ "$CONFIRM" != "Y" ]; then
  echo "Aborted."
  exit 0
fi

#------------------------------------------------------------------------------
# Phase 3: Ensure state bucket exists
#------------------------------------------------------------------------------

echo ""
echo "=== Phase 3: Ensuring state bucket exists ==="

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
  echo "  State bucket created."
fi

#------------------------------------------------------------------------------
# Phase 4: Bootstrap deploy-runner CodeBuild project
#------------------------------------------------------------------------------

echo ""
echo "=== Phase 4: Bootstrapping deploy-runner ==="

if aws codebuild batch-get-projects --names "$CODEBUILD_PROJECT" --region "$AWS_REGION" \
    --query 'projects[0].name' --output text 2>/dev/null | grep -q "$CODEBUILD_PROJECT"; then
  echo "  CodeBuild project exists: $CODEBUILD_PROJECT"
  echo "  Updating source location..."
  aws codebuild update-project \
    --name "$CODEBUILD_PROJECT" \
    --region "$AWS_REGION" \
    --source '{
      "type": "S3",
      "location": "'"$STATE_BUCKET"'/deploy-source/source.zip",
      "buildspec": "buildspec-deploy.yml"
    }' \
    > /dev/null
else
  echo "  Creating CodeBuild project: $CODEBUILD_PROJECT"

  # Create IAM role for CodeBuild
  ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${IAM_ROLE_NAME}"
  if aws iam get-role --role-name "$IAM_ROLE_NAME" &>/dev/null; then
    echo "  IAM role exists: $IAM_ROLE_NAME"
    ROLE_ARN="$(aws iam get-role --role-name "$IAM_ROLE_NAME" --query 'Role.Arn' --output text)"
  else
    echo "  Creating IAM role: $IAM_ROLE_NAME"
    ROLE_ARN="$(aws iam create-role \
      --role-name "$IAM_ROLE_NAME" \
      --assume-role-policy-document '{
        "Version": "2012-10-17",
        "Statement": [{
          "Effect": "Allow",
          "Principal": {"Service": "codebuild.amazonaws.com"},
          "Action": "sts:AssumeRole"
        }]
      }' \
      --query 'Role.Arn' --output text)"

    aws iam attach-role-policy \
      --role-name "$IAM_ROLE_NAME" \
      --policy-arn "arn:aws:iam::aws:policy/AdministratorAccess"

    echo "  Waiting for IAM role to propagate..."
    sleep 10
  fi

  # Create CodeBuild project
  aws codebuild create-project \
    --name "$CODEBUILD_PROJECT" \
    --region "$AWS_REGION" \
    --source '{
      "type": "S3",
      "location": "'"$STATE_BUCKET"'/deploy-source/source.zip",
      "buildspec": "buildspec-deploy.yml"
    }' \
    --artifacts '{"type": "NO_ARTIFACTS"}' \
    --environment '{
      "type": "ARM_CONTAINER",
      "image": "'"$CODEBUILD_IMAGE"'",
      "computeType": "'"$CODEBUILD_COMPUTE"'",
      "privilegedMode": true
    }' \
    --service-role "$ROLE_ARN" \
    --timeout-in-minutes 120 \
    --logs-config '{
      "cloudWatchLogs": {"status": "ENABLED"}
    }' \
    > /dev/null

  echo "  CodeBuild project created."
fi

#------------------------------------------------------------------------------
# Phase 5: Zip and upload source to S3
#------------------------------------------------------------------------------

echo ""
echo "=== Phase 5: Uploading source to S3 ==="

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
echo "  Source uploaded to s3://$STATE_BUCKET/deploy-source/source.zip"

#------------------------------------------------------------------------------
# Phase 6: Start CodeBuild build
#------------------------------------------------------------------------------

echo ""
echo "=== Phase 6: Starting deploy build ==="

# Check for in-progress builds (scan recent builds, not just the latest)
BUILD_ID=""
RECENT_BUILDS="$(aws codebuild list-builds-for-project \
  --project-name "$CODEBUILD_PROJECT" \
  --region "$AWS_REGION" \
  --query 'ids' \
  --output json 2>/dev/null || echo '[]')"

ACTIVE_BUILD="$(echo "$RECENT_BUILDS" | python3 -c "
import sys, json, subprocess
ids = json.load(sys.stdin)
for build_id in ids[:10]:
    result = subprocess.run(
        ['aws', 'codebuild', 'batch-get-builds', '--ids', build_id,
         '--region', '${AWS_REGION}', '--query', 'builds[0].buildStatus', '--output', 'text'],
        capture_output=True, text=True
    )
    if result.stdout.strip() == 'IN_PROGRESS':
        print(build_id)
        break
" 2>/dev/null || echo "")"

if [ -n "$ACTIVE_BUILD" ]; then
  echo "  A build is already in progress: $ACTIVE_BUILD"
  printf "  Wait for it instead of starting a new one? (Y/n): "
  read -r WAIT_CHOICE
  if [ "$WAIT_CHOICE" != "n" ] && [ "$WAIT_CHOICE" != "N" ]; then
    BUILD_ID="$ACTIVE_BUILD"
    echo "  Attaching to existing build..."
  fi
fi

if [ -z "$BUILD_ID" ]; then
  # Resolve caller's IAM role ARN for EKS access grants.
  # For assumed-role ARNs (arn:aws:sts::ACCT:assumed-role/RoleName/session),
  # look up the full IAM role ARN via get-role. SSO roles include a path
  # (e.g. /aws-reserved/sso.amazonaws.com/REGION/) that EKS requires.
  CALLER_ROLE_ARN=""
  if echo "$IDENTITY_ARN" | grep -q "assumed-role"; then
    ROLE_NAME="$(echo "$IDENTITY_ARN" | cut -d'/' -f2)"
    CALLER_ROLE_ARN="$(aws iam get-role --role-name "$ROLE_NAME" --query 'Role.Arn' --output text 2>/dev/null || true)"
  fi

  ENV_OVERRIDES='[
    {"name":"ACTION","value":"deploy","type":"PLAINTEXT"},
    {"name":"AWS_REGION","value":"'"$AWS_REGION"'","type":"PLAINTEXT"}
  '
  if [ -n "$CALLER_ROLE_ARN" ]; then
    ENV_OVERRIDES+='  ,{"name":"CALLER_ROLE_ARN","value":"'"$CALLER_ROLE_ARN"'","type":"PLAINTEXT"}'
  fi
  ENV_OVERRIDES+=']'

  BUILD_ID="$(aws codebuild start-build \
    --project-name "$CODEBUILD_PROJECT" \
    --region "$AWS_REGION" \
    --environment-variables-override "$ENV_OVERRIDES" \
    --query 'build.id' \
    --output text)"
fi

echo "  Build ID: $BUILD_ID"

# Print console URL for convenience
BUILD_UUID="${BUILD_ID#*:}"
echo "  Console:  https://${AWS_REGION}.console.aws.amazon.com/codesuite/codebuild/${ACCOUNT_ID}/projects/${CODEBUILD_PROJECT}/build/${CODEBUILD_PROJECT}%3A${BUILD_UUID}"
echo ""
echo "  Waiting for build to complete..."

#------------------------------------------------------------------------------
# Phase 7: Poll build status and tail logs
#------------------------------------------------------------------------------

LOG_GROUP=""
LOG_STREAM=""
LOG_TOKEN=""

while true; do
  BUILD_STATUS="$(aws codebuild batch-get-builds \
    --ids "$BUILD_ID" \
    --region "$AWS_REGION" \
    --query 'builds[0].buildStatus' \
    --output text)"

  # Try to get log group/stream for tailing
  if [ -z "$LOG_GROUP" ]; then
    LOG_GROUP="$(aws codebuild batch-get-builds \
      --ids "$BUILD_ID" \
      --region "$AWS_REGION" \
      --query 'builds[0].logs.groupName' \
      --output text 2>/dev/null || echo "")"
    LOG_STREAM="$(aws codebuild batch-get-builds \
      --ids "$BUILD_ID" \
      --region "$AWS_REGION" \
      --query 'builds[0].logs.streamName' \
      --output text 2>/dev/null || echo "")"
    # --output text returns "None" for null values
    [ "$LOG_GROUP" = "None" ] && LOG_GROUP=""
    [ "$LOG_STREAM" = "None" ] && LOG_STREAM=""
  fi

  # Tail CloudWatch logs if available
  if [ -n "$LOG_GROUP" ] && [ -n "$LOG_STREAM" ]; then
    LOG_ARGS=(
      --log-group-name "$LOG_GROUP"
      --log-stream-name "$LOG_STREAM"
      --start-from-head
      --region "$AWS_REGION"
    )
    if [ -n "$LOG_TOKEN" ]; then
      LOG_ARGS+=(--next-token "$LOG_TOKEN")
    fi

    LOG_OUTPUT="$(aws logs get-log-events "${LOG_ARGS[@]}" --output json 2>/dev/null || echo '{}')"

    # Print new log messages
    NEW_TOKEN="$(echo "$LOG_OUTPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('nextForwardToken',''))" 2>/dev/null || echo "")"
    if [ -n "$NEW_TOKEN" ] && [ "$NEW_TOKEN" != "$LOG_TOKEN" ]; then
      echo "$LOG_OUTPUT" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for event in data.get('events', []):
    print('  [build] ' + event.get('message', '').rstrip())
" 2>/dev/null || true
      LOG_TOKEN="$NEW_TOKEN"
    fi
  fi

  case "$BUILD_STATUS" in
    SUCCEEDED)
      echo ""
      echo "  Build SUCCEEDED."
      break
      ;;
    FAILED|FAULT|STOPPED|TIMED_OUT)
      echo ""
      echo "ERROR: Build $BUILD_STATUS."
      echo "  Check logs: aws codebuild batch-get-builds --ids $BUILD_ID --region $AWS_REGION"
      exit 1
      ;;
    *)
      sleep 15
      ;;
  esac
done

#------------------------------------------------------------------------------
# Phase 8: Read outputs and create initial user
#------------------------------------------------------------------------------

echo ""
echo "=== Phase 8: Post-deploy ==="

APP_URL="$(aws ssm get-parameter --name /eval-managed/app-url --region "$AWS_REGION" --query 'Parameter.Value' --output text 2>/dev/null || echo "")"
COGNITO_POOL_ID="$(aws ssm get-parameter --name /eval-managed/cognito-user-pool-id --region "$AWS_REGION" --query 'Parameter.Value' --output text 2>/dev/null || echo "")"

if [ -n "$COGNITO_POOL_ID" ]; then
  USER_COUNT="$(aws cognito-idp list-users --user-pool-id "$COGNITO_POOL_ID" --region "$AWS_REGION" --query 'length(Users)' --output text 2>/dev/null || echo "0")"

  if [ "$USER_COUNT" -gt 0 ] 2>/dev/null; then
    echo "  Users already exist ($USER_COUNT found). Skipping initial user setup."
    echo "  Manage users with: ./manage-users.sh"
  else
    echo ""
    echo "  No users found. The application requires Cognito users (no self-signup)."
    echo "  You can create an initial admin user now, or skip and use manage-users.sh later."
    echo ""
    printf "  Enter email for initial admin user (or press Enter to skip): "
    read -r INITIAL_USER_EMAIL

    if [ -n "$INITIAL_USER_EMAIL" ]; then
      TEMP_PASSWORD="$(openssl rand -base64 12 | tr -d '/+=' | head -c 12)Aa1!"

      echo ""
      echo "  Creating user: $INITIAL_USER_EMAIL"
      aws cognito-idp admin-create-user \
        --user-pool-id "$COGNITO_POOL_ID" \
        --username "$INITIAL_USER_EMAIL" \
        --user-attributes '[{"Name":"email","Value":"'"$INITIAL_USER_EMAIL"'"},{"Name":"email_verified","Value":"true"}]' \
        --temporary-password "$TEMP_PASSWORD" \
        --message-action SUPPRESS \
        --region "$AWS_REGION" \
        > /dev/null

      echo "  User created successfully."
      echo ""
      echo "  ┌─────────────────────────────────────────────────────┐"
      echo "  │  Initial User Credentials                           │"
      echo "  │                                                     │"
      printf "  │  Email:    %-40s │\n" "$INITIAL_USER_EMAIL"
      printf "  │  Password: %-40s │\n" "$TEMP_PASSWORD"
      echo "  │                                                     │"
      echo "  │  The user must change the password on first login.  │"
      echo "  └─────────────────────────────────────────────────────┘"
    else
      echo ""
      echo "  Skipped. Create users later with:"
      echo "    ./manage-users.sh create user@example.com"
    fi
  fi
fi

#------------------------------------------------------------------------------
# Phase 9: Summary
#------------------------------------------------------------------------------

echo ""
echo "=============================================="
echo "  Deployment complete!"
echo "=============================================="
echo ""
if [ -n "$APP_URL" ]; then
  echo "  App URL: $APP_URL"
else
  echo "  App URL: (check SSM parameter /eval-managed/app-url)"
fi
echo ""
echo "  Note: CloudFront may take 5-10 minutes to fully propagate on first deploy."
echo ""
echo "  Manage users:"
echo "    ./manage-users.sh create user@example.com"
echo "    ./manage-users.sh list"
echo "    ./manage-users.sh delete user@example.com"
echo ""
echo "  To update after code changes, re-run: ./deploy.sh"
echo "  To tear down (preserving data): ./destroy.sh"
echo ""
