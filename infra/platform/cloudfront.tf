#------------------------------------------------------------------------------
# WAF (us-east-1)
#------------------------------------------------------------------------------

resource "aws_wafv2_web_acl" "main" {
  provider = aws.virginia
  name     = local.name
  scope    = "CLOUDFRONT"

  default_action {
    allow {}
  }

  rule {
    name     = "RateLimit"
    priority = 1
    action {
      block {}
    }
    statement {
      rate_based_statement {
        limit              = 2000
        aggregate_key_type = "IP"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name}-rate"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "AWSCommon"
    priority = 2
    override_action {
      none {}
    }
    statement {
      managed_rule_group_statement {
        vendor_name = "AWS"
        name        = "AWSManagedRulesCommonRuleSet"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name}-common"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "AWSKnownBadInputs"
    priority = 3
    override_action {
      none {}
    }
    statement {
      managed_rule_group_statement {
        vendor_name = "AWS"
        name        = "AWSManagedRulesKnownBadInputsRuleSet"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name}-bad-inputs"
      sampled_requests_enabled   = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = local.name
    sampled_requests_enabled   = true
  }
}

#------------------------------------------------------------------------------
# WAF Logging
#------------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "waf" {
  provider          = aws.virginia
  name              = "aws-waf-logs-${local.name}"
  retention_in_days = 14
}

resource "aws_wafv2_web_acl_logging_configuration" "main" {
  provider                = aws.virginia
  log_destination_configs = [aws_cloudwatch_log_group.waf.arn]
  resource_arn            = aws_wafv2_web_acl.main.arn

  logging_filter {
    default_behavior = "DROP"
    filter {
      behavior    = "KEEP"
      requirement = "MEETS_ANY"
      condition {
        action_condition { action = "BLOCK" }
      }
    }
  }
}

#------------------------------------------------------------------------------
# CloudFront
#------------------------------------------------------------------------------

resource "aws_cloudfront_vpc_origin" "alb" {
  vpc_origin_endpoint_config {
    name                   = local.name
    arn                    = module.alb.arn
    http_port              = 80
    https_port             = 443
    origin_protocol_policy = "http-only"

    origin_ssl_protocols {
      items    = ["TLSv1.2"]
      quantity = 1
    }
  }
}

data "aws_cloudfront_cache_policy" "disabled" {
  name = "Managed-CachingDisabled"
}

data "aws_cloudfront_cache_policy" "optimized" {
  name = "Managed-CachingOptimized"
}

data "aws_cloudfront_origin_request_policy" "all_viewer" {
  name = "Managed-AllViewer"
}

#------------------------------------------------------------------------------
# Origin Access Control (OAC) for the private SPA S3 bucket
#
# OAC is the current AWS-recommended mechanism (OAI is legacy). signing_behavior
# "always" makes every CloudFront->S3 request SigV4-signed over HTTPS, so the
# bucket can stay fully private (all public access blocked) and is readable
# ONLY by this distribution (enforced by the bucket policy's AWS:SourceArn
# condition in spa_bucket_policy.tf).
#------------------------------------------------------------------------------

resource "aws_cloudfront_origin_access_control" "spa" {
  name                              = "${local.name}-spa"
  description                       = "OAC for the static SPA bucket"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

#------------------------------------------------------------------------------
# SPA client-side-routing fallback (CloudFront Function, viewer-request)
#
# Mirrors the local nginx `try_files $uri /index.html`. Deep links like
# /history have no object in S3, so rewrite extension-less, non-gated paths to
# /index.html and let react-router resolve them. The gated prefixes
# (/api,/inspect,/oauth2,/health) are separate ALB behaviors and never reach
# this function (it's attached to the default S3 behavior only); the explicit
# guard below is defense-in-depth so a path can NEVER be rewritten away from
# the ALB origin onto the public S3 bucket. /assets and any path with a file
# extension pass through untouched so real S3 objects are served as-is.
#------------------------------------------------------------------------------

resource "aws_cloudfront_function" "spa_router" {
  name    = "${local.name}-spa-router"
  runtime = "cloudfront-js-2.0"
  comment = "SPA deep-link fallback to /index.html"
  publish = true
  code    = <<-EOT
    function handler(event) {
      var request = event.request;
      var uri = request.uri;
      // Never touch gated/backend paths (defensive — these are separate
      // ALB behaviors and shouldn't reach this function).
      if (uri.startsWith('/api') || uri.startsWith('/inspect') ||
          uri.startsWith('/oauth2') || uri === '/health') {
        return request;
      }
      // Real static assets (hashed bundle) and any path with a file
      // extension are served from S3 as-is.
      if (uri.startsWith('/assets/') || uri.split('/').pop().indexOf('.') !== -1) {
        return request;
      }
      // SPA route (/, /chat, /history, …) -> serve the app shell.
      request.uri = '/index.html';
      return request;
    }
  EOT
}

resource "aws_cloudfront_distribution" "main" {
  enabled         = true
  http_version    = "http2and3"
  is_ipv6_enabled = true
  price_class     = "PriceClass_100"
  web_acl_id      = aws_wafv2_web_acl.main.arn

  # Dynamic / authenticated origin: the internal ALB (-> oauth2-proxy -> backend).
  origin {
    domain_name = module.alb.dns_name
    origin_id   = "alb"
    vpc_origin_config {
      vpc_origin_id            = aws_cloudfront_vpc_origin.alb.id
      origin_read_timeout      = 60
      origin_keepalive_timeout = 5
    }
  }

  # Static origin: the private SPA bucket, read via OAC only.
  origin {
    domain_name              = var.spa_bucket_regional_domain_name
    origin_id                = "spa-s3"
    origin_access_control_id = aws_cloudfront_origin_access_control.spa.id
  }

  # DEFAULT behavior -> S3 (the public SPA shell). Mirrors the local nginx
  # model where static is the catch-all and gated paths are explicitly
  # proxied. The SPA-router function provides client-side-routing fallback.
  # S3 holds no credentials or tenant data, so a routing mistake here can only
  # ever serve the public shell / a 404 — never leak data.
  default_cache_behavior {
    target_origin_id       = "spa-s3"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true
    cache_policy_id        = data.aws_cloudfront_cache_policy.optimized.id

    function_association {
      event_type   = "viewer-request"
      function_arn = aws_cloudfront_function.spa_router.arn
    }
  }

  # Hashed, content-addressed assets -> S3, cached hard.
  ordered_cache_behavior {
    path_pattern           = "/assets/*"
    target_origin_id       = "spa-s3"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true
    cache_policy_id        = data.aws_cloudfront_cache_policy.optimized.id
  }

  # ---- Gated / dynamic paths -> ALB -> oauth2-proxy -> backend (fail-safe). ----
  # Each must be an explicit behavior so it is NEVER served from the public S3
  # default. Caching disabled + all methods + all-viewer so auth headers,
  # cookies, and POST/SSE pass through to oauth2-proxy intact.
  ordered_cache_behavior {
    path_pattern             = "/api/*"
    target_origin_id         = "alb"
    viewer_protocol_policy   = "redirect-to-https"
    allowed_methods          = ["DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"]
    cached_methods           = ["GET", "HEAD"]
    compress                 = true
    cache_policy_id          = data.aws_cloudfront_cache_policy.disabled.id
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer.id
  }

  ordered_cache_behavior {
    path_pattern             = "/inspect/*"
    target_origin_id         = "alb"
    viewer_protocol_policy   = "redirect-to-https"
    allowed_methods          = ["DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"]
    cached_methods           = ["GET", "HEAD"]
    compress                 = true
    cache_policy_id          = data.aws_cloudfront_cache_policy.disabled.id
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer.id
  }

  ordered_cache_behavior {
    path_pattern             = "/oauth2/*"
    target_origin_id         = "alb"
    viewer_protocol_policy   = "redirect-to-https"
    allowed_methods          = ["DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"]
    cached_methods           = ["GET", "HEAD"]
    compress                 = true
    cache_policy_id          = data.aws_cloudfront_cache_policy.disabled.id
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer.id
  }

  ordered_cache_behavior {
    path_pattern             = "/health"
    target_origin_id         = "alb"
    viewer_protocol_policy   = "redirect-to-https"
    allowed_methods          = ["GET", "HEAD"]
    cached_methods           = ["GET", "HEAD"]
    compress                 = true
    cache_policy_id          = data.aws_cloudfront_cache_policy.disabled.id
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer.id
  }

  restrictions {
    geo_restriction { restriction_type = "none" }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
    minimum_protocol_version       = "TLSv1.2_2021"
  }
}
