#------------------------------------------------------------------------------
# ECR
#------------------------------------------------------------------------------

module "ecr" {
  source  = "terraform-aws-modules/ecr/aws"
  version = "~> 2.0"

  repository_name                 = "${local.name}-${local.region_suffix}/app"
  repository_force_delete         = true
  repository_image_scan_on_push   = true
  repository_image_tag_mutability = "MUTABLE" # Allow overwriting latest tags

  repository_lifecycle_policy = jsonencode({
    rules = [{
      rulePriority = 1
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 10 }
      action       = { type = "expire" }
    }]
  })
}
