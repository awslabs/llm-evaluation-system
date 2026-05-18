terraform {
  required_version = ">= 1.10.0"

  backend "s3" {
    key          = "platform/terraform.tfstate"
    use_lockfile = true
    encrypt      = true
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.25"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.12"
    }
    kubectl = {
      source = "alekc/kubectl"
      # Pin to 2.1.x — v2.2.0 ships an incompatible provider schema
      # (gRPC v6 attributes nil → fully populated) that surfaces in
      # CodeBuild as: "Invalid Provider Server Combination: differing
      # provider schema implementations across providers". CodeBuild runs
      # `terraform init -upgrade` which ignores the lockfile, so the
      # constraint here must exclude 2.2.x.
      version = "~> 2.1.3"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.0"
    }
  }
}

#------------------------------------------------------------------------------
# Providers
#------------------------------------------------------------------------------

provider "aws" {
  region      = var.region
  max_retries = 15
  retry_mode  = "adaptive"

  default_tags {
    tags = {
      Project   = var.project_name
      ManagedBy = "terraform"
      Layer     = "platform"
    }
  }
}

provider "aws" {
  # us-east-1 is required by AWS for CloudFront WAF rules and ECR Public
  alias       = "virginia"
  region      = "us-east-1"
  max_retries = 15
  retry_mode  = "adaptive"

  default_tags {
    tags = {
      Project   = var.project_name
      ManagedBy = "terraform"
      Layer     = "platform"
    }
  }
}

provider "kubernetes" {
  host                   = module.eks.cluster_endpoint
  cluster_ca_certificate = base64decode(module.eks.cluster_certificate_authority_data)

  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = ["eks", "get-token", "--cluster-name", module.eks.cluster_name, "--region", var.region]
  }
}

provider "helm" {
  kubernetes {
    host                   = module.eks.cluster_endpoint
    cluster_ca_certificate = base64decode(module.eks.cluster_certificate_authority_data)

    exec {
      api_version = "client.authentication.k8s.io/v1beta1"
      command     = "aws"
      args        = ["eks", "get-token", "--cluster-name", module.eks.cluster_name, "--region", var.region]
    }
  }
}

provider "kubectl" {
  host                   = module.eks.cluster_endpoint
  cluster_ca_certificate = base64decode(module.eks.cluster_certificate_authority_data)
  load_config_file       = false

  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = ["eks", "get-token", "--cluster-name", module.eks.cluster_name, "--region", var.region]
  }
}

#------------------------------------------------------------------------------
# Data Sources
#------------------------------------------------------------------------------

data "aws_caller_identity" "current" {}
data "aws_ecrpublic_authorization_token" "token" { provider = aws.virginia }

data "aws_ec2_managed_prefix_list" "cloudfront" {
  name = "com.amazonaws.global.cloudfront.origin-facing"
}

data "aws_secretsmanager_secret_version" "oidc_idp" {
  count     = var.enable_oidc_idp ? 1 : 0
  secret_id = var.oidc_client_secret_arn
}

#------------------------------------------------------------------------------
# Locals
#------------------------------------------------------------------------------

locals {
  account_id    = data.aws_caller_identity.current.account_id
  name          = var.project_name
  region_suffix = replace(var.region, "-", "")
}
