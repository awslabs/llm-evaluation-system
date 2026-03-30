terraform {
  required_version = ">= 1.10.0"

  backend "s3" {
    key          = "data/terraform.tfstate"
    use_lockfile = true
    encrypt      = true
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
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
      Layer     = "data"
    }
  }
}

#------------------------------------------------------------------------------
# Data Sources
#------------------------------------------------------------------------------

data "aws_caller_identity" "current" {}
data "aws_availability_zones" "available" { state = "available" }

#------------------------------------------------------------------------------
# Locals
#------------------------------------------------------------------------------

locals {
  account_id = data.aws_caller_identity.current.account_id
  azs        = slice(data.aws_availability_zones.available.names, 0, 2)
  name       = var.project_name
}
