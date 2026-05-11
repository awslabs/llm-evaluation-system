output "bucket_name" {
  description = "The name of the eval logs bucket"
  value       = module.eval_logs_bucket.s3_bucket_id
}

output "bucket_arn" {
  description = "The ARN of the eval logs bucket"
  value       = module.eval_logs_bucket.s3_bucket_arn
}

output "bucket_region" {
  description = "The region of the eval logs bucket"
  value       = module.eval_logs_bucket.s3_bucket_region
}
