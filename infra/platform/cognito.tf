#------------------------------------------------------------------------------
# Cognito User Pool
#
# Default: Native Cognito auth (admin-created users, email/password)
# Optional: External OIDC IdP (Okta, Azure AD, Amazon Federate, etc.)
#           enabled via enable_oidc_idp = true
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

#------------------------------------------------------------------------------
# External OIDC Identity Provider (optional)
#------------------------------------------------------------------------------

resource "aws_cognito_identity_provider" "oidc" {
  count = var.enable_oidc_idp ? 1 : 0

  user_pool_id  = aws_cognito_user_pool.main.id
  provider_name = var.oidc_provider_name
  provider_type = "OIDC"

  provider_details = {
    client_id                 = var.oidc_client_id
    client_secret             = data.aws_secretsmanager_secret_version.oidc_idp[0].secret_string
    oidc_issuer               = var.oidc_issuer_url
    authorize_scopes          = "openid email profile"
    attributes_request_method = "GET"
  }

  attribute_mapping = {
    email              = "email"
    given_name         = "given_name"
    preferred_username = "preferred_username"
    username           = "sub"
  }
}

#------------------------------------------------------------------------------
# Cognito User Pool Client
#------------------------------------------------------------------------------

resource "aws_cognito_user_pool_client" "main" {
  name         = local.name
  user_pool_id = aws_cognito_user_pool.main.id

  generate_secret                      = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_scopes                 = ["email", "openid", "profile"]

  supported_identity_providers = var.enable_oidc_idp ? ["COGNITO", var.oidc_provider_name] : ["COGNITO"]

  callback_urls = [
    "https://${aws_cloudfront_distribution.main.domain_name}/oauth2/callback"
  ]
  logout_urls = [
    "https://${aws_cloudfront_distribution.main.domain_name}/",
    "https://${aws_cloudfront_distribution.main.domain_name}/oauth2/sign_out",
  ]

  access_token_validity  = 10
  id_token_validity      = 60
  refresh_token_validity = 30

  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "days"
  }

  prevent_user_existence_errors = "ENABLED"

  depends_on = [aws_cognito_identity_provider.oidc]
}

#------------------------------------------------------------------------------
# Secrets (Cognito client secret + oauth2-proxy)
#------------------------------------------------------------------------------

resource "aws_secretsmanager_secret" "cognito_client" {
  name                    = "${local.name}/cognito-client-secret"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "cognito_client" {
  secret_id     = aws_secretsmanager_secret.cognito_client.id
  secret_string = jsonencode({ clientSecret = aws_cognito_user_pool_client.main.client_secret })
}

#------------------------------------------------------------------------------
# oauth2-proxy Secrets
#------------------------------------------------------------------------------

resource "random_password" "oauth2_proxy_cookie_secret" {
  length  = 32
  special = false
}

resource "aws_secretsmanager_secret" "oauth2_proxy" {
  name                    = "${local.name}/oauth2-proxy-secrets"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "oauth2_proxy" {
  secret_id = aws_secretsmanager_secret.oauth2_proxy.id
  secret_string = jsonencode({
    clientId     = aws_cognito_user_pool_client.main.id
    clientSecret = aws_cognito_user_pool_client.main.client_secret
    cookieSecret = base64encode(random_password.oauth2_proxy_cookie_secret.result)
  })
}
