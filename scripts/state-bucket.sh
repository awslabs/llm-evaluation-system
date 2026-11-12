#!/usr/bin/env bash
#------------------------------------------------------------------------------
# state-bucket.sh — owner-checked access to S3 buckets used by the deploy scripts
#
# Source this file. Requires: STATE_BUCKET, ACCOUNT_ID, AWS_REGION.
#
# S3 bucket names are global and STATE_BUCKET is predictable from the account
# ID and region, so another AWS account could create it first. Every call here
# pins the owner with --expected-bucket-owner so we never upload source, read
# state, or run a build from a bucket this account does not own.
#------------------------------------------------------------------------------

# _bucket_status <bucket>
# Prints "owned" or "missing". Exits 1 if the bucket is owned by another
# account or the check fails for any other reason.
_bucket_status() {
  local bucket="$1" err
  if err="$(aws s3api head-bucket --bucket "$bucket" \
      --expected-bucket-owner "$ACCOUNT_ID" --region "$AWS_REGION" 2>&1 >/dev/null)"; then
    echo "owned"
  elif grep -q "(404)" <<<"$err"; then
    echo "missing"
  else
    echo "ERROR: Could not verify that S3 bucket '$bucket' is owned by account $ACCOUNT_ID." >&2
    echo "  $err" >&2
    echo "  If the bucket exists in another AWS account, do not use it. Contact the" >&2
    echo "  maintainers before deploying." >&2
    exit 1
  fi
}

_state_bucket_status() { _bucket_status "$STATE_BUCKET"; }

# require_owned_bucket <bucket>
# Abort unless the bucket exists and this account owns it.
require_owned_bucket() {
  [ "$(_bucket_status "$1")" = "owned" ] || {
    echo "ERROR: S3 bucket $1 does not exist in account $ACCOUNT_ID." >&2
    exit 1
  }
}

# Use the bucket if this account owns it, create it if it does not exist,
# abort if it exists in another account.
ensure_state_bucket() {
  local status
  status="$(_state_bucket_status)"
  if [ "$status" = "owned" ]; then
    echo "  State bucket exists (owned by $ACCOUNT_ID): $STATE_BUCKET"
    return 0
  fi

  echo "  Creating state bucket: $STATE_BUCKET"
  # create-bucket fails if another account takes the name in the meantime.
  if [ "$AWS_REGION" = "us-east-1" ]; then
    aws s3api create-bucket --bucket "$STATE_BUCKET" --region "$AWS_REGION" > /dev/null
  else
    aws s3api create-bucket --bucket "$STATE_BUCKET" --region "$AWS_REGION" \
      --create-bucket-configuration LocationConstraint="$AWS_REGION" > /dev/null
  fi
  aws s3api put-bucket-versioning --bucket "$STATE_BUCKET" \
    --expected-bucket-owner "$ACCOUNT_ID" --region "$AWS_REGION" \
    --versioning-configuration Status=Enabled
  aws s3api put-public-access-block --bucket "$STATE_BUCKET" \
    --expected-bucket-owner "$ACCOUNT_ID" --region "$AWS_REGION" \
    --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
  # Confirm ownership before anything is written to it.
  [ "$(_state_bucket_status)" = "owned" ] || {
    echo "ERROR: State bucket $STATE_BUCKET is not owned by $ACCOUNT_ID after creation." >&2
    exit 1
  }
  echo "  State bucket created with versioning enabled."
}

# Abort unless the bucket exists and this account owns it.
require_state_bucket() {
  local status
  status="$(_state_bucket_status)"
  if [ "$status" != "owned" ]; then
    echo "ERROR: State bucket $STATE_BUCKET does not exist in account $ACCOUNT_ID." >&2
    echo "  Nothing to destroy, or deploy.sh has not been run in this account/region." >&2
    exit 1
  fi
  echo "  State bucket verified (owned by $ACCOUNT_ID): $STATE_BUCKET"
}

# upload_to_state_bucket <local-file> <key>
# `aws s3 cp` has no owner check, so use put-object.
upload_to_state_bucket() {
  aws s3api put-object --bucket "$STATE_BUCKET" --key "$2" --body "$1" \
    --expected-bucket-owner "$ACCOUNT_ID" --region "$AWS_REGION" > /dev/null
}
