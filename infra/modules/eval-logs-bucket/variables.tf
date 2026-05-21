variable "bucket_name" {
  description = "Logical name for the S3 bucket. The module appends the caller's AWS account ID to guarantee global uniqueness — final bucket is <bucket_name>-<account_id>."
  type        = string
}

variable "tags" {
  description = "Additional tags for the bucket"
  type        = map(string)
  default     = {}
}
