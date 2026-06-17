#------------------------------------------------------------------------------
# Cognito User Pool — DURABLE IDENTITY STORE
#
# Lives in the DATA layer (not platform) because a user pool holds user
# accounts — irreplaceable state, exactly like RDS rows and S3 objects. The
# platform layer is destroyed/recreated by destroy.sh; the data layer is
# preserved. Keeping the pool here means a platform teardown no longer wipes
# user accounts, honoring destroy.sh's contract ("preserves data layer").
#
# Only the pool + its hosted-UI domain live here (no cross-layer deps). The
# user-pool CLIENT, secrets, hosted-UI CSS, and optional OIDC IdP stay in the
# platform layer: the client's callback/logout URLs reference the CloudFront
# distribution (platform), and those resources hold no user data and are cheap
# to recreate per deploy. The platform layer consumes the pool id/arn/endpoint
# as -var inputs (same data->platform thread as RDS and the S3 buckets).
#------------------------------------------------------------------------------

resource "aws_cognito_user_pool" "main" {
  name = local.name

  # Admin-only signup — no self-registration regardless of auth mode
  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  mfa_configuration = "OFF"

  account_recovery_setting {
    recovery_mechanism {
      name     = "admin_only"
      priority = 1
    }
  }
}

resource "aws_cognito_user_pool_domain" "main" {
  domain       = "${local.name}-${local.region_suffix}-${local.account_id}"
  user_pool_id = aws_cognito_user_pool.main.id
}
