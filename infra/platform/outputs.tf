output "update_kubeconfig" {
  description = "Update kubeconfig command"
  value       = "aws eks update-kubeconfig --name ${module.eks.cluster_name} --region ${var.region}"
}

output "app_url" {
  description = "Application URL"
  value       = "https://${aws_cloudfront_distribution.main.domain_name}"
}

output "cloudfront_distribution_id" {
  description = "CloudFront distribution ID (for SPA cache invalidation on deploy)"
  value       = aws_cloudfront_distribution.main.id
}

output "cognito_idp_response_url" {
  description = "OIDC IdP response URL — configure this as the redirect URI in your identity provider"
  value       = var.enable_oidc_idp ? "https://${aws_cognito_user_pool_domain.main.domain}.auth.${var.region}.amazoncognito.com/oauth2/idpresponse" : null
}

output "ecr_repository" {
  description = "ECR repository URL"
  value       = module.ecr.repository_url
}

output "cognito_user_pool_id" {
  description = "Cognito User Pool ID"
  value       = aws_cognito_user_pool.main.id
}

output "eks_cluster_name" {
  description = "EKS cluster name"
  value       = module.eks.cluster_name
}

#------------------------------------------------------------------------------
# CI/CD
#------------------------------------------------------------------------------

output "source_bucket" {
  description = "S3 bucket for source code"
  value       = module.source_bucket.s3_bucket_id
}

output "codebuild_project" {
  description = "CodeBuild project name"
  value       = aws_codebuild_project.image_build.name
}

output "build_commands" {
  description = "Commands to trigger a build"
  value       = <<-EOF
    zip -r /tmp/source.zip . -x "*.git*" -x "*/node_modules/*" -x "*/.next/*" -x "*.terraform*" -x "*/.venv/*" -x "*.tfstate*"
    aws s3 cp /tmp/source.zip s3://${module.source_bucket.s3_bucket_id}/source.zip
    aws codebuild start-build --project-name ${aws_codebuild_project.image_build.name}
  EOF
}
