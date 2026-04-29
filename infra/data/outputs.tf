#------------------------------------------------------------------------------
# VPC
#------------------------------------------------------------------------------

output "vpc_id" {
  description = "VPC ID"
  value       = module.vpc.vpc_id
}

output "vpc_cidr_block" {
  description = "VPC CIDR block"
  value       = module.vpc.vpc_cidr_block
}

output "private_subnets" {
  description = "Private subnet IDs"
  value       = module.vpc.private_subnets
}

output "public_subnets" {
  description = "Public subnet IDs"
  value       = module.vpc.public_subnets
}

output "intra_subnets" {
  description = "Intra subnet IDs"
  value       = module.vpc.intra_subnets
}

#------------------------------------------------------------------------------
# RDS
#------------------------------------------------------------------------------

output "rds_endpoint" {
  description = "RDS endpoint address"
  value       = module.rds.db_instance_address
}

output "rds_identifier" {
  description = "RDS instance identifier"
  value       = module.rds.db_instance_identifier
}

output "rds_secret_arn" {
  description = "RDS master password secret ARN in Secrets Manager"
  value       = module.rds.db_instance_master_user_secret_arn
}

output "rds_security_group_id" {
  description = "RDS security group ID"
  value       = aws_security_group.rds.id
}

output "rds_resource_id" {
  description = "RDS DBI resource ID (for IAM auth)"
  value       = module.rds.db_instance_resource_id
}

#------------------------------------------------------------------------------
# S3
#------------------------------------------------------------------------------

output "documents_bucket" {
  description = "S3 documents bucket name"
  value       = module.documents_bucket.s3_bucket_id
}

output "documents_bucket_arn" {
  description = "S3 documents bucket ARN"
  value       = module.documents_bucket.s3_bucket_arn
}

output "data_bucket" {
  description = "S3 data bucket name (eval logs, judges, datasets, configs)"
  value       = module.data_bucket.s3_bucket_id
}

output "data_bucket_arn" {
  description = "S3 data bucket ARN"
  value       = module.data_bucket.s3_bucket_arn
}
