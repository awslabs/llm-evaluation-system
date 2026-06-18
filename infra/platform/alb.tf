#------------------------------------------------------------------------------
# Internal ALB for CloudFront VPC Origin
#------------------------------------------------------------------------------

resource "aws_security_group" "alb" {
  name   = "${local.name}-alb"
  vpc_id = var.vpc_id

  ingress {
    from_port       = 80
    to_port         = 80
    protocol        = "tcp"
    prefix_list_ids = [data.aws_ec2_managed_prefix_list.cloudfront.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group_rule" "alb_to_eks" {
  type                     = "ingress"
  from_port                = 0
  to_port                  = 65535
  protocol                 = "tcp"
  source_security_group_id = aws_security_group.alb.id
  security_group_id        = module.eks.node_security_group_id
  description              = "Allow ALB to reach EKS nodes"
}

module "alb" {
  source  = "terraform-aws-modules/alb/aws"
  version = "~> 9.0"

  name                       = local.name
  internal                   = true
  load_balancer_type         = "application"
  vpc_id                     = var.vpc_id
  subnets                    = var.private_subnets
  security_groups            = [aws_security_group.alb.id]
  idle_timeout               = 120
  enable_deletion_protection = false

  listeners = {
    http = {
      port     = 80
      protocol = "HTTP"
      forward  = { target_group_key = "oauth2proxy" }

      rules = {
        # Health check bypass (no auth needed). The backend /health handler
        # ignores the user header, so this is inert — it's the ONLY rule that
        # forwards to the backend TG directly. Everything else goes through
        # oauth2-proxy (the default forward), which is what makes the backend's
        # X-Forwarded-User trust safe. The SPA + its assets are served by
        # CloudFront -> S3 and never reach the ALB.
        health = { priority = 1, conditions = [{ path_pattern = { values = ["/health"] } }], actions = [{ type = "forward", target_group_key = "backend" }] }
        # Everything else through oauth2-proxy (protected routes: /, /chat, /api/*, /inspect/*, /oauth2/*)
      }
    }
  }

  target_groups = {
    oauth2proxy = {
      name              = "${local.name}-oauth2proxy"
      protocol          = "HTTP"
      port              = 4180
      target_type       = "ip"
      create_attachment = false
      health_check      = { path = "/ping", interval = 15 }
    }
    backend = {
      name              = "${local.name}-backend"
      protocol          = "HTTP"
      port              = 8080
      target_type       = "ip"
      create_attachment = false
      health_check      = { path = "/health", interval = 15 }
      stickiness        = { enabled = true, type = "lb_cookie", cookie_duration = 3600 }
    }
  }
}
