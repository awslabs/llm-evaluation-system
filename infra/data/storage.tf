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
# SQLite Backup Bucket (periodic backup to S3)
# NOTE: Module named "litestream_bucket" for terraform state compatibility
# Bucket name kept as "-litestream-" to avoid recreating existing bucket
#------------------------------------------------------------------------------

module "litestream_bucket" {
  source  = "terraform-aws-modules/s3-bucket/aws"
  version = "~> 4.0"

  bucket        = "${local.name}-litestream-${local.account_id}"
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
