#------------------------------------------------------------------------------
# Namespace
#------------------------------------------------------------------------------

resource "kubernetes_namespace" "app" {
  metadata { name = "eval-managed" }
  depends_on = [null_resource.wait_for_cluster, module.eks]
}

#------------------------------------------------------------------------------
# ConfigMap - Non-sensitive configuration
#------------------------------------------------------------------------------

resource "kubernetes_config_map" "app_config" {
  metadata {
    name      = "app-config"
    namespace = kubernetes_namespace.app.metadata[0].name
  }

  data = {
    # Database (POSTGRES_* naming expected by backend)
    POSTGRES_HOST         = var.rds_endpoint
    POSTGRES_PORT         = "5432"
    POSTGRES_DB           = "chat_db"
    POSTGRES_USER         = "backend" # IAM auth user (not master postgres user)
    POSTGRES_USE_IAM_AUTH = "true"    # Enable IAM token authentication

    # Cognito / OIDC — pool id/domain come from the data layer (durable
    # identity store); the client is platform-owned (per-deployment).
    COGNITO_USER_POOL_ID = var.cognito_user_pool_id
    COGNITO_CLIENT_ID    = aws_cognito_user_pool_client.main.id
    COGNITO_DOMAIN       = "${var.cognito_user_pool_domain}.auth.${var.region}.amazoncognito.com"
    OIDC_ISSUER          = "https://cognito-idp.${var.region}.amazonaws.com/${var.cognito_user_pool_id}"
    OIDC_CLIENT_ID       = aws_cognito_user_pool_client.main.id

    # Storage — S3 is primary store for all persistent data
    S3_BUCKET   = var.documents_bucket
    DATA_BUCKET = var.data_bucket

    # AWS
    AWS_REGION = var.region

    # URLs
    CLOUDFRONT_DOMAIN = aws_cloudfront_distribution.main.domain_name
    APP_URL           = "https://${aws_cloudfront_distribution.main.domain_name}"

    # Inspect AI configuration
    INSPECT_LOG_LEVEL = "info"
  }
}

#------------------------------------------------------------------------------
# External Secrets Config - SecretStore and ExternalSecrets via Helm CLI
#------------------------------------------------------------------------------

resource "helm_release" "external_secrets_config" {
  name      = "external-secrets-config"
  chart     = "${path.module}/../../helm/external-secrets-config"
  namespace = local.name
  wait      = true
  timeout   = 300

  depends_on = [helm_release.external_secrets, kubernetes_namespace.app, module.eks]

  set {
    name  = "region"
    value = var.region
  }
  set {
    name  = "namespace"
    value = local.name
  }
  set {
    name  = "dbSecretArn"
    value = var.rds_secret_arn
  }
  set {
    name  = "cognitoSecretName"
    value = aws_secretsmanager_secret.cognito_client.name
  }
  set {
    name  = "oauth2ProxySecretName"
    value = aws_secretsmanager_secret.oauth2_proxy.name
  }
  set {
    name  = "llmProviderKeysSecretName"
    value = "${local.name}/llm-provider-keys"
  }
}

#------------------------------------------------------------------------------
# LLM Provider Keys Secret (optional — users populate via AWS console/CLI)
# Holds OPENAI_API_KEY, ANTHROPIC_API_KEY, GOOGLE_API_KEY for external providers
#------------------------------------------------------------------------------

resource "aws_secretsmanager_secret" "llm_provider_keys" {
  name                    = "${local.name}/llm-provider-keys"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "llm_provider_keys" {
  secret_id = aws_secretsmanager_secret.llm_provider_keys.id
  secret_string = jsonencode({
    OPENAI_API_KEY    = ""
    ANTHROPIC_API_KEY = ""
    GOOGLE_API_KEY    = ""
  })

  lifecycle {
    ignore_changes = [secret_string]
  }
}

#------------------------------------------------------------------------------
# External Secrets Operator
#------------------------------------------------------------------------------

resource "helm_release" "external_secrets" {
  name             = "external-secrets"
  repository       = "https://charts.external-secrets.io"
  chart            = "external-secrets"
  namespace        = "external-secrets"
  create_namespace = true
  wait             = true
  wait_for_jobs    = true
  timeout          = 300
  # Wait for pod identity so pods start with correct credentials - no restart needed
  # ALB controller webhook must be ready before any helm release that creates Services
  depends_on = [null_resource.wait_for_cluster, module.external_secrets_pod_identity, helm_release.alb_controller, module.eks]
}

#------------------------------------------------------------------------------
# Service Accounts with Pod Identity
#------------------------------------------------------------------------------

module "external_secrets_pod_identity" {
  source  = "terraform-aws-modules/eks-pod-identity/aws"
  version = "~> 1.0"

  name = "${local.name}-external-secrets"

  attach_external_secrets_policy        = true
  external_secrets_ssm_parameter_arns   = ["arn:aws:ssm:${var.region}:${local.account_id}:parameter/${local.name}/*"]
  external_secrets_secrets_manager_arns = [var.rds_secret_arn, aws_secretsmanager_secret.cognito_client.arn, aws_secretsmanager_secret.oauth2_proxy.arn, aws_secretsmanager_secret.llm_provider_keys.arn]

  # Pod Identity for the External Secrets Operator controller (runs in external-secrets namespace)
  associations = {
    main = {
      cluster_name    = module.eks.cluster_name
      namespace       = "external-secrets"
      service_account = "external-secrets"
    }
  }
}


module "backend_pod_identity" {
  source  = "terraform-aws-modules/eks-pod-identity/aws"
  version = "~> 1.0"

  name = "${local.name}-backend"

  additional_policy_arns = { backend = aws_iam_policy.backend.arn }

  associations = {
    main = {
      cluster_name    = module.eks.cluster_name
      namespace       = kubernetes_namespace.app.metadata[0].name
      service_account = "backend-sa"
    }
  }
}

resource "aws_iam_policy" "backend" {
  name = "${local.name}-backend"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"]
        Resource = [var.documents_bucket_arn, "${var.documents_bucket_arn}/*"]
      },
      {
        # Primary data bucket — eval logs, judges, datasets, configs
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket", "s3:GetBucketLocation"]
        Resource = [var.data_bucket_arn, "${var.data_bucket_arn}/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream", "bedrock:ListFoundationModels", "bedrock:ListInferenceProfiles"]
        Resource = "*"
      },
      {
        # Bedrock Mantle — OpenAI frontier models (GPT-5.4/5.5) are served on the
        # bedrock-mantle endpoint (OpenAI-compatible Responses API), not
        # bedrock-runtime. Inspect mints a short-lived bearer token from this
        # role's credentials and calls Mantle (bedrock-mantle:CreateInference /
        # CallWithBearerToken). Account must also be entitled
        # (model access / C-score); run-time validation surfaces a clear error
        # if not.
        Effect = "Allow"
        Action = [
          "bedrock-mantle:CreateInference",
          "bedrock-mantle:Get*",
          "bedrock-mantle:List*",
          "bedrock-mantle:CallWithBearerToken",
        ]
        Resource = "*"
      },
      {
        # IAM database authentication - pods connect to RDS using IAM tokens instead of passwords
        Effect   = "Allow"
        Action   = ["rds-db:connect"]
        Resource = "arn:aws:rds-db:${var.region}:${local.account_id}:dbuser:${var.rds_resource_id}/backend"
      }
    ]
  })
}

resource "kubernetes_service_account" "backend" {
  metadata {
    name      = "backend-sa"
    namespace = kubernetes_namespace.app.metadata[0].name
  }
  depends_on = [module.backend_pod_identity, module.eks]
}

#------------------------------------------------------------------------------
# IAM Database User Setup Job
# Creates the 'backend' PostgreSQL user with rds_iam role for IAM authentication
#------------------------------------------------------------------------------

resource "kubernetes_job" "setup_iam_db_user" {
  metadata {
    name      = "setup-iam-db-user"
    namespace = kubernetes_namespace.app.metadata[0].name
  }

  spec {
    ttl_seconds_after_finished = 300 # Clean up after 5 minutes
    backoff_limit              = 3

    template {
      metadata {
        labels = {
          app = "setup-iam-db-user"
        }
      }

      spec {
        restart_policy = "OnFailure"

        container {
          name  = "setup"
          image = "postgres:16-alpine"

          env {
            name  = "PGHOST"
            value = var.rds_endpoint
          }
          env {
            name  = "PGPORT"
            value = "5432"
          }
          env {
            name  = "PGDATABASE"
            value = "chat_db"
          }
          env {
            name  = "PGUSER"
            value = "postgres"
          }
          env {
            name = "PGPASSWORD"
            value_from {
              secret_key_ref {
                name = "db-credentials"
                key  = "POSTGRES_PASSWORD"
              }
            }
          }

          command = ["/bin/sh", "-c"]
          args = [<<-EOF
            set -e
            echo "Creating IAM database user 'backend'..."

            # Create user if not exists
            psql -c "SELECT 1 FROM pg_roles WHERE rolname = 'backend'" | grep -q 1 || psql -c "CREATE USER backend;"

            # Grant IAM role and schema permissions
            psql -c "GRANT rds_iam TO backend;"
            psql -c "GRANT ALL PRIVILEGES ON DATABASE chat_db TO backend;"
            psql -c "GRANT ALL ON SCHEMA public TO backend;"
            psql -c "GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO backend;"
            psql -c "GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO backend;"
            psql -c "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO backend;"
            psql -c "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO backend;"

            # Transfer ownership of existing tables to backend user
            for table in users chat_sessions messages; do
              psql -c "ALTER TABLE IF EXISTS $table OWNER TO backend;" 2>/dev/null || true
            done

            # Verify setup
            echo "Verifying user setup..."
            psql -c "SELECT rolname, rolcanlogin FROM pg_roles WHERE rolname = 'backend';"

            echo "IAM database user setup complete."
          EOF
          ]
        }
      }
    }
  }

  wait_for_completion = true
  timeouts {
    create = "5m"
  }

  depends_on = [
    helm_release.external_secrets_config, # Need db-credentials secret to exist
    module.eks,
  ]
}

#------------------------------------------------------------------------------
# Target Group Bindings
#------------------------------------------------------------------------------

resource "kubectl_manifest" "tgb_backend" {
  yaml_body  = <<-YAML
    apiVersion: elbv2.k8s.aws/v1beta1
    kind: TargetGroupBinding
    metadata:
      name: backend-tgb
      namespace: eval-managed
    spec:
      serviceRef:
        name: backend
        port: 8080
      targetGroupARN: ${module.alb.target_groups["backend"].arn}
      targetType: ip
  YAML
  depends_on = [kubernetes_namespace.app, helm_release.alb_controller, module.eks]
}

resource "kubectl_manifest" "tgb_oauth2proxy" {
  yaml_body  = <<-YAML
    apiVersion: elbv2.k8s.aws/v1beta1
    kind: TargetGroupBinding
    metadata:
      name: oauth2proxy-tgb
      namespace: eval-managed
    spec:
      serviceRef:
        name: oauth2-proxy
        port: 4180
      targetGroupARN: ${module.alb.target_groups["oauth2proxy"].arn}
      targetType: ip
  YAML
  depends_on = [kubernetes_namespace.app, helm_release.alb_controller, module.eks]
}
