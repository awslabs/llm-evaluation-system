variable "project_name" {
  description = "Project name prefix for all resources"
  type        = string
  default     = "eval-managed"
}

variable "region" {
  description = "Primary AWS region for all core infrastructure"
  type        = string
  default     = "us-west-2"
}
