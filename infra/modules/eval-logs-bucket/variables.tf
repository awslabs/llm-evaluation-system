variable "bucket_name" {
  description = "Name for the S3 bucket that stores eval logs"
  type        = string
}

variable "tags" {
  description = "Additional tags for the bucket"
  type        = map(string)
  default     = {}
}
