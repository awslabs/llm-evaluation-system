#------------------------------------------------------------------------------
# Cognito — per-deployment OAuth client config
#
# The USER POOL + hosted-UI DOMAIN now live in the DATA layer (durable identity
# store; see infra/data/cognito.tf) and are passed in as vars so a platform
# teardown no longer destroys user accounts. This file owns only the per-
# deployment pieces: the user-pool CLIENT (its callback/logout URLs reference
# the CloudFront distribution, which is platform-owned), the client secret,
# the oauth2-proxy secrets, the hosted-UI CSS, and the optional OIDC IdP. None
# of these hold user data and all are cheap to recreate per deploy.
#
# Default: Native Cognito auth (admin-created users, email/password)
# Optional: External OIDC IdP (Okta, Azure AD, Amazon Federate, etc.)
#           enabled via enable_oidc_idp = true
#------------------------------------------------------------------------------

#------------------------------------------------------------------------------
# External OIDC Identity Provider (optional)
#------------------------------------------------------------------------------

resource "aws_cognito_identity_provider" "oidc" {
  count = var.enable_oidc_idp ? 1 : 0

  user_pool_id  = var.cognito_user_pool_id
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
  user_pool_id = var.cognito_user_pool_id

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

#------------------------------------------------------------------------------
# Cognito Hosted UI customization
#
# Paint-job only. AWS still owns the password flow, form layout, MFA,
# brute-force protection, OIDC compliance, and token issuance. We only
# upload a CSS string (Cognito enforces a whitelist of selectors and
# properties) so the hosted login page renders in the Observatory palette
# instead of stock white. No security boundary moved.
#
# The whitelist means this CSS cannot:
#   - inject scripts
#   - hide or modify the password field
#   - change the form action or redirect target
#   - capture credentials
# Cognito validates the CSS server-side before serving it.
#------------------------------------------------------------------------------

resource "aws_cognito_user_pool_ui_customization" "main" {
  user_pool_id = var.cognito_user_pool_id
  client_id    = aws_cognito_user_pool_client.main.id

  css = <<-CSS
    .background-customizable {
      background-color: #0c0a08;
      background-image: linear-gradient(180deg, #15120e 0%, #0c0a08 100%);
    }
    .banner-customizable {
      background-color: #0c0a08;
      padding: 32px 0 16px;
    }
    .logo-customizable {
      max-width: 0;
      max-height: 0;
    }
    .label-customizable {
      color: #ece6d8;
      font-weight: 500;
      letter-spacing: 0.02em;
    }
    .textDescription-customizable {
      color: #a39a87;
    }
    .idpDescription-customizable {
      color: #a39a87;
    }
    .legalText-customizable {
      color: #6f6759;
      font-size: 11px;
    }
    .inputField-customizable {
      background-color: #15120e;
      color: #ece6d8;
      border: 1px solid #2a241d;
      border-radius: 2px;
      padding: 12px 14px;
    }
    .inputField-customizable:focus {
      border-color: #d97757;
      outline: none;
    }
    .submitButton-customizable {
      background-color: #d97757;
      color: #0c0a08;
      border: 1px solid #d97757;
      border-radius: 2px;
      font-weight: 600;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      padding: 12px 18px;
    }
    .submitButton-customizable:hover {
      background-color: #c25a36;
      border-color: #c25a36;
      color: #0c0a08;
    }
    .errorMessage-customizable {
      background-color: #3a1f15;
      color: #d97757;
      border: 1px solid #a35336;
    }
    .idpButton-customizable {
      background-color: #15120e;
      color: #ece6d8;
      border: 1px solid #2a241d;
    }
    .idpButton-customizable:hover {
      background-color: #1d1812;
      border-color: #a39a87;
    }
    .socialButton-customizable {
      background-color: #15120e;
      color: #ece6d8;
      border: 1px solid #2a241d;
    }
    .redirect-customizable {
      color: #d97757;
    }
    .passwordCheck-notValid-customizable {
      color: #c4524d;
    }
    .passwordCheck-valid-customizable {
      color: #9bb556;
    }
  CSS
}
