#!/usr/bin/env bash
set -euo pipefail

#------------------------------------------------------------------------------
# manage-users.sh — Manage Cognito users for the LLM Evaluation Platform
#
# Usage:
#   ./manage-users.sh create user@example.com
#   ./manage-users.sh list
#   ./manage-users.sh delete user@example.com
#------------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

#------------------------------------------------------------------------------
# Usage
#------------------------------------------------------------------------------

usage() {
  echo "Usage: ./manage-users.sh <command> [args]"
  echo ""
  echo "Commands:"
  echo "  create <email>   Create a user and display temporary password"
  echo "  list             List all users in the pool"
  echo "  delete <email>   Delete a user (with confirmation)"
  echo ""
  exit 1
}

#------------------------------------------------------------------------------
# Validate AWS credentials
#------------------------------------------------------------------------------

validate_aws() {
  if [ -z "${AWS_PROFILE:-}" ] && [ -z "${AWS_ACCESS_KEY_ID:-}" ]; then
    echo "No AWS_PROFILE or AWS_ACCESS_KEY_ID set."
    echo ""
    echo "  Available profiles:"
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

  if [ -z "${AWS_REGION:-}" ]; then
    AWS_REGION="$(aws configure get region 2>/dev/null || true)"
    if [ -z "$AWS_REGION" ]; then
      echo "ERROR: AWS_REGION not set and no default region configured."
      echo "Set it with: export AWS_REGION=us-west-2"
      exit 1
    fi
    export AWS_REGION
  fi
}

#------------------------------------------------------------------------------
# Resolve Cognito Pool ID
#------------------------------------------------------------------------------

resolve_pool() {
  validate_aws

  COGNITO_POOL_ID="$(aws ssm get-parameter \
    --name /eval-managed/cognito-user-pool-id \
    --region "$AWS_REGION" \
    --query 'Parameter.Value' \
    --output text 2>/dev/null)" || {
    echo "ERROR: Could not read Cognito pool ID from SSM parameter /eval-managed/cognito-user-pool-id."
    echo "Make sure you have deployed with ./deploy.sh first."
    exit 1
  }

  echo "  Pool:   $COGNITO_POOL_ID"
  echo "  Region: $AWS_REGION"
  echo ""
}

#------------------------------------------------------------------------------
# create
#------------------------------------------------------------------------------

cmd_create() {
  local email="${1:-}"
  if [ -z "$email" ]; then
    echo "ERROR: Email address required."
    echo "Usage: ./manage-users.sh create user@example.com"
    exit 1
  fi

  resolve_pool

  TEMP_PASSWORD="$(openssl rand -base64 12 | tr -d '/+=' | head -c 12)Aa1!"

  echo "  Creating user: $email"
  aws cognito-idp admin-create-user \
    --user-pool-id "$COGNITO_POOL_ID" \
    --username "$email" \
    --user-attributes '[{"Name":"email","Value":"'"$email"'"},{"Name":"email_verified","Value":"true"}]' \
    --temporary-password "$TEMP_PASSWORD" \
    --message-action SUPPRESS \
    --region "$AWS_REGION" \
    > /dev/null

  echo "  User created successfully."
  echo ""
  echo "  ┌─────────────────────────────────────────────────────┐"
  echo "  │  Credentials                                        │"
  echo "  │                                                     │"
  printf "  │  Email:    %-40s │\n" "$email"
  printf "  │  Password: %-40s │\n" "$TEMP_PASSWORD"
  echo "  │                                                     │"
  echo "  │  The user must change the password on first login.  │"
  echo "  └─────────────────────────────────────────────────────┘"
}

#------------------------------------------------------------------------------
# list
#------------------------------------------------------------------------------

cmd_list() {
  resolve_pool

  echo "  Users:"
  echo ""

  aws cognito-idp list-users \
    --user-pool-id "$COGNITO_POOL_ID" \
    --region "$AWS_REGION" \
    --query 'Users[].{Email:Attributes[?Name==`email`].Value|[0],Status:UserStatus,Created:UserCreateDate}' \
    --output table
}

#------------------------------------------------------------------------------
# delete
#------------------------------------------------------------------------------

cmd_delete() {
  local email="${1:-}"
  if [ -z "$email" ]; then
    echo "ERROR: Email address required."
    echo "Usage: ./manage-users.sh delete user@example.com"
    exit 1
  fi

  resolve_pool

  printf "  Delete user '%s'? (y/N): " "$email"
  read -r CONFIRM
  if [ "$CONFIRM" != "y" ] && [ "$CONFIRM" != "Y" ]; then
    echo "  Aborted."
    exit 0
  fi

  aws cognito-idp admin-delete-user \
    --user-pool-id "$COGNITO_POOL_ID" \
    --username "$email" \
    --region "$AWS_REGION"

  echo "  User '$email' deleted."
}

#------------------------------------------------------------------------------
# Main
#------------------------------------------------------------------------------

COMMAND="${1:-}"
shift || true

case "$COMMAND" in
  create) cmd_create "$@" ;;
  list)   cmd_list "$@" ;;
  delete) cmd_delete "$@" ;;
  *)      usage ;;
esac
