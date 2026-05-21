#!/usr/bin/env bash
# Create the eval-mcp team-sharing S3 bucket.
#
# Wraps `terraform init` + `terraform apply` with the same AWS_PROFILE +
# region prompts as deploy.sh / destroy.sh / manage-users.sh, so users
# (especially SSO users) don't hit cryptic "No valid credential sources
# found" errors from terraform itself.
#
# Usage:
#   ./create-bucket.sh                    # prompts for everything missing
#   ./create-bucket.sh my-team-evals      # uses given logical name
#   AWS_PROFILE=foo ./create-bucket.sh    # skips profile prompt

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

#------------------------------------------------------------------------------
# AWS credentials
#------------------------------------------------------------------------------

if [ -z "${AWS_PROFILE:-}" ] && [ -z "${AWS_ACCESS_KEY_ID:-}" ]; then
  echo "No AWS_PROFILE set. Available profiles:"
  aws configure list-profiles 2>/dev/null | sed 's/^/    /'
  echo ""
  printf "Enter AWS profile name: "
  read -r PROFILE_INPUT
  if [ -z "$PROFILE_INPUT" ]; then
    echo "ERROR: No profile specified." >&2
    exit 1
  fi
  export AWS_PROFILE="$PROFILE_INPUT"
fi

if ! aws sts get-caller-identity >/dev/null 2>&1; then
  echo "ERROR: AWS credentials not usable for profile '${AWS_PROFILE:-(default)}'." >&2
  if [ -n "${AWS_PROFILE:-}" ]; then
    echo "If using SSO, refresh with: aws sso login --profile $AWS_PROFILE" >&2
  fi
  exit 1
fi

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
IDENTITY_ARN="$(aws sts get-caller-identity --query Arn --output text)"

#------------------------------------------------------------------------------
# Region
#------------------------------------------------------------------------------

if [ -z "${AWS_REGION:-}" ]; then
  AWS_REGION="$(aws configure get region 2>/dev/null || true)"
fi
if [ -z "$AWS_REGION" ]; then
  printf "AWS region for the bucket (e.g. us-west-2): "
  read -r AWS_REGION
  if [ -z "$AWS_REGION" ]; then
    echo "ERROR: No region specified." >&2
    exit 1
  fi
fi
export AWS_REGION

#------------------------------------------------------------------------------
# Bucket name
#------------------------------------------------------------------------------

BUCKET_NAME="${1:-}"
if [ -z "$BUCKET_NAME" ]; then
  printf "Logical bucket name (account-ID will be appended) [my-team-evals]: "
  read -r BUCKET_NAME
  BUCKET_NAME="${BUCKET_NAME:-my-team-evals}"
fi

#------------------------------------------------------------------------------
# Plan + apply
#------------------------------------------------------------------------------

echo ""
echo "Plan:"
echo "  Profile:    ${AWS_PROFILE:-(env credentials)}"
echo "  Identity:   $IDENTITY_ARN"
echo "  Region:     $AWS_REGION"
echo "  Bucket:     ${BUCKET_NAME}-${ACCOUNT_ID}"
echo ""

terraform init -upgrade
terraform apply -var="bucket_name=$BUCKET_NAME" -var="region=$AWS_REGION"

echo ""
echo "Done. Next: uvx --from llm-evaluation-system eval-mcp init $BUCKET_NAME"
