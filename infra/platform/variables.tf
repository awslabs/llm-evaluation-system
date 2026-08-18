# Validation on project_name / region / vpc_id is a security control, not just
# input hygiene: all three reach a `local-exec` provisioner in eks.tf. Those
# provisioners now pass values through the `environment` map rather than
# interpolating them into the shell command (the primary CWE-78 fix), and these
# constraints are the second layer — a value that cannot contain a shell
# metacharacter cannot be an injection vector even if a future edit reintroduces
# string interpolation. tests/test_terraform_provisioner_safety.py guards the
# first layer; keep both.

variable "project_name" {
  description = "Project name prefix for all resources"
  type        = string
  default     = "eval-managed"

  validation {
    # Also the character set AWS accepts for the resource names this prefixes
    # (EKS cluster, IAM roles, S3-adjacent names), so this is not purely a
    # security bound.
    condition     = can(regex("^[a-z0-9][a-z0-9-]{0,30}[a-z0-9]$", var.project_name))
    error_message = "project_name must be 2-32 characters of lowercase letters, digits and hyphens, starting and ending with a letter or digit."
  }
}

variable "region" {
  description = "Primary AWS region for EKS and all core infrastructure"
  type        = string
  # deploy.sh always writes an explicit `region = ...` into the tfvars (it errors
  # out if AWS_REGION is unset), so this default is a fallback for direct
  # terraform invocation only. us-east-2 matches the Bedrock default in
  # eval_mcp/core/bedrock_client.py: it is the region carrying the full model
  # set, including the OpenAI frontier models (gpt-5.5, gpt-5.6-sol) that AWS
  # serves only in us-east-1/us-east-2.
  default = "us-east-2"

  validation {
    # Allows an optional middle segment so the GovCloud and China partitions
    # still validate (us-gov-west-1, cn-north-1). A stricter
    # `^[a-z]{2}-[a-z]+-[0-9]+$` would reject those and break those consumers.
    condition     = can(regex("^[a-z]{2}(-[a-z]+){1,2}-[0-9]+$", var.region))
    error_message = "region must be a valid AWS region name, e.g. us-east-2, eu-central-1 or us-gov-west-1."
  }
}

#------------------------------------------------------------------------------
# Authentication
#------------------------------------------------------------------------------

variable "enable_oidc_idp" {
  description = "Enable an external OIDC identity provider (e.g., Okta, Azure AD, Amazon Federate). When false, uses Cognito native email/password auth."
  type        = bool
  default     = false
}

variable "oidc_provider_name" {
  description = "Name for the OIDC identity provider in Cognito (no spaces, used as provider identifier)"
  type        = string
  default     = "ExternalOIDC"
}

variable "oidc_client_id" {
  description = "OIDC client ID from your identity provider (required when enable_oidc_idp = true)"
  type        = string
  default     = ""
}

variable "oidc_client_secret_arn" {
  description = "ARN of the OIDC client secret in AWS Secrets Manager (required when enable_oidc_idp = true)"
  type        = string
  default     = ""
}

variable "oidc_issuer_url" {
  description = "OIDC issuer URL from your identity provider (required when enable_oidc_idp = true)"
  type        = string
  default     = ""
}

#------------------------------------------------------------------------------
# EKS
#------------------------------------------------------------------------------

variable "eks_cluster_version" {
  description = "Kubernetes version"
  type        = string
  default     = "1.34"
}

variable "cluster_admin_role_arns" {
  description = "IAM role ARNs for EKS admin access"
  type        = list(string)
  default     = []
}

#------------------------------------------------------------------------------
# Data Layer Inputs (passed from infra/data terraform outputs)
#------------------------------------------------------------------------------

variable "vpc_id" {
  description = "VPC ID from data layer"
  type        = string

  validation {
    # AWS VPC IDs are `vpc-` plus 8 (legacy) or 17 (current) hex characters.
    # Reaches a local-exec provisioner in eks.tf — see the note at the top.
    condition     = can(regex("^vpc-[0-9a-f]{8,17}$", var.vpc_id))
    error_message = "vpc_id must be a valid VPC ID: 'vpc-' followed by 8 or 17 hexadecimal characters."
  }
}

variable "vpc_cidr_block" {
  description = "VPC CIDR block from data layer"
  type        = string
}

variable "private_subnets" {
  description = "Private subnet IDs from data layer"
  type        = list(string)
}

variable "public_subnets" {
  description = "Public subnet IDs from data layer"
  type        = list(string)
}

variable "intra_subnets" {
  description = "Intra subnet IDs from data layer"
  type        = list(string)
}

variable "rds_endpoint" {
  description = "RDS endpoint address from data layer"
  type        = string
}

variable "rds_secret_arn" {
  description = "RDS master password secret ARN from data layer"
  type        = string
}

variable "rds_security_group_id" {
  description = "RDS security group ID from data layer"
  type        = string
}

variable "rds_resource_id" {
  description = "RDS DBI resource ID for IAM auth from data layer"
  type        = string
}

variable "documents_bucket" {
  description = "S3 documents bucket name from data layer"
  type        = string
}

variable "documents_bucket_arn" {
  description = "S3 documents bucket ARN from data layer"
  type        = string
}

variable "data_bucket" {
  description = "S3 data bucket name (eval logs, judges, datasets, configs)"
  type        = string
}

variable "data_bucket_arn" {
  description = "S3 data bucket ARN"
  type        = string
}

variable "spa_bucket" {
  description = "S3 SPA bucket name (static Vite frontend bundle) from data layer"
  type        = string
}

variable "spa_bucket_arn" {
  description = "S3 SPA bucket ARN from data layer"
  type        = string
}

variable "spa_bucket_regional_domain_name" {
  description = "S3 SPA bucket regional domain name (CloudFront S3 origin) from data layer"
  type        = string
}

variable "cognito_user_pool_id" {
  description = "Cognito user pool ID from data layer (durable identity store)"
  type        = string
}

variable "cognito_user_pool_arn" {
  description = "Cognito user pool ARN from data layer"
  type        = string
}

variable "cognito_user_pool_domain" {
  description = "Cognito hosted-UI domain prefix from data layer"
  type        = string
}
