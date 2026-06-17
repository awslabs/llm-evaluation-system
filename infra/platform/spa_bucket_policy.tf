#------------------------------------------------------------------------------
# SPA bucket policy — grant CloudFront OAC read-only access
#
# The SPA bucket (created in the data layer) is fully private. This policy is
# the ONLY thing that lets anything read it, and it allows just the CloudFront
# service principal, scoped via AWS:SourceArn to THIS distribution — so no
# other distribution, account, or principal can read the bundle. This is the
# exact pattern AWS documents for OAC. The policy lives in the platform layer
# (not data) because it must reference the distribution ARN; this also breaks
# the OAC<->distribution dependency cycle.
#------------------------------------------------------------------------------

data "aws_iam_policy_document" "spa_bucket" {
  statement {
    sid       = "AllowCloudFrontServicePrincipalReadOnly"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["${var.spa_bucket_arn}/*"]

    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.main.arn]
    }
  }
}

resource "aws_s3_bucket_policy" "spa" {
  bucket = var.spa_bucket
  policy = data.aws_iam_policy_document.spa_bucket.json
}
