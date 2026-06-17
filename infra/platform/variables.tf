variable "project_name" {
  description = "Project name prefix for all resources"
  type        = string
  default     = "eval-managed"
}

variable "region" {
  description = "Primary AWS region for EKS and all core infrastructure"
  type        = string
  default     = "us-west-2"
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
