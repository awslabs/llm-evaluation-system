module "rds" {
  source  = "terraform-aws-modules/rds/aws"
  version = "~> 6.0"

  identifier = local.name

  engine               = "postgres"
  engine_version       = "16"
  family               = "postgres16"
  major_engine_version = "16"
  instance_class       = "db.t3.micro"

  allocated_storage     = 20
  max_allocated_storage = 100

  db_name  = "chat_db"
  username = "postgres"
  port     = 5432

  manage_master_user_password = true
  # Disable auto-rotation - master password only used for admin/setup tasks
  # App uses IAM auth tokens, not passwords. Rotation breaks External Secrets sync.
  manage_master_user_password_rotation = false

  # Enable IAM database authentication - pods authenticate with IAM tokens instead of passwords
  iam_database_authentication_enabled = true

  vpc_security_group_ids = [aws_security_group.rds.id]
  create_db_subnet_group = true
  subnet_ids             = module.vpc.private_subnets

  backup_retention_period     = 7
  storage_encrypted           = true
  deletion_protection         = false
  skip_final_snapshot         = true
  allow_major_version_upgrade = true
  apply_immediately           = true

  performance_insights_enabled = true
}

resource "aws_security_group" "rds" {
  name   = "${local.name}-rds"
  vpc_id = module.vpc.vpc_id

  ingress {
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = [module.vpc.vpc_cidr_block]
  }
}
