#------------------------------------------------------------------------------
# S3 Documents Bucket
#------------------------------------------------------------------------------

module "documents_bucket" {
  source  = "terraform-aws-modules/s3-bucket/aws"
  version = "~> 4.0"

  bucket        = "${local.name}-documents-${local.account_id}"
  force_destroy = true

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true

  versioning = { enabled = true }

  server_side_encryption_configuration = {
    rule = {
      apply_server_side_encryption_by_default = { sse_algorithm = "AES256" }
    }
  }

  cors_rule = [{
    allowed_headers = ["*"]
    allowed_methods = ["GET", "PUT", "POST"]
    allowed_origins = ["*"]
    expose_headers  = ["ETag"]
  }]

  attach_deny_insecure_transport_policy = true
}

#------------------------------------------------------------------------------
# Data Bucket — primary store for eval logs, judges, datasets, configs
#------------------------------------------------------------------------------

module "data_bucket" {
  source  = "terraform-aws-modules/s3-bucket/aws"
  version = "~> 4.0"

  bucket        = "${local.name}-data-${local.account_id}"
  force_destroy = true

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true

  versioning = { enabled = true }

  server_side_encryption_configuration = {
    rule = {
      apply_server_side_encryption_by_default = { sse_algorithm = "AES256" }
    }
  }

  attach_deny_insecure_transport_policy = true
}

#------------------------------------------------------------------------------
# SPA Bucket — static Vite frontend bundle, served via CloudFront OAC
#
# Fully private (all public access blocked). The browser NEVER talks to this
# bucket directly — only CloudFront does, server-to-server, signed via OAC —
# so there is intentionally NO cors_rule (unlike documents_bucket). The OAC
# bucket policy that grants the CloudFront distribution read access lives in
# the platform layer (infra/platform), because it must reference the
# distribution ARN; keeping the bucket here (data layer) gives it a stable
# name across platform destroy/redeploy and breaks the OAC<->distribution
# dependency cycle.
#------------------------------------------------------------------------------

module "spa_bucket" {
  source  = "terraform-aws-modules/s3-bucket/aws"
  version = "~> 4.0"

  bucket        = "${local.name}-spa-${local.account_id}"
  force_destroy = true

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true

  versioning = { enabled = true }

  server_side_encryption_configuration = {
    rule = {
      apply_server_side_encryption_by_default = { sse_algorithm = "AES256" }
    }
  }

  attach_deny_insecure_transport_policy = true
}
