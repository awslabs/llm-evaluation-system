variable "project_name" {
  description = "Project name prefix for all resources"
  type        = string
  default     = "eval-managed"
}

variable "region" {
  description = "Primary AWS region for all core infrastructure"
  type        = string
  # deploy.sh always writes an explicit `region = ...` into the tfvars (it errors
  # out if AWS_REGION is unset), so this default is a fallback for direct
  # terraform invocation only. us-east-2 matches the Bedrock default in
  # eval_mcp/core/bedrock_client.py: it is the region carrying the full model
  # set, including the OpenAI frontier models (gpt-5.5, gpt-5.6-sol) that AWS
  # serves only in us-east-1/us-east-2.
  default = "us-east-2"
}
