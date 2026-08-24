variable "aws_region" {
  description = "AWS Region in which to deploy the discovery slice."
  type        = string
  default     = "eu-west-1"
}

variable "project_name" {
  description = "Short project name used in resource names and tags."
  type        = string
  default     = "filing-corpus-pipeline"

  validation {
    condition     = can(regex("^[a-z0-9-]{3,30}$", var.project_name))
    error_message = "project_name must contain 3-30 lowercase letters, digits, or hyphens."
  }
}

variable "environment" {
  description = "Deployment environment name."
  type        = string
  default     = "dev"

  validation {
    condition     = can(regex("^[a-z0-9-]{2,12}$", var.environment))
    error_message = "environment must contain 2-12 lowercase letters, digits, or hyphens."
  }
}

variable "sec_user_agent" {
  description = "SEC-compliant application identity and monitored contact address."
  type        = string
  sensitive   = true

  validation {
    condition = (
      length(trimspace(var.sec_user_agent)) >= 8 &&
      strcontains(var.sec_user_agent, "@")
    )
    error_message = "sec_user_agent must identify the application and include a monitored email address."
  }
}

variable "issuer_ids" {
  description = "SEC CIKs scanned by each scheduled discovery execution."
  type        = list(string)
  default     = ["0000320193", "0000789019"]

  validation {
    condition = (
      length(var.issuer_ids) > 0 &&
      length(var.issuer_ids) <= 5 &&
      length(distinct(var.issuer_ids)) == length(var.issuer_ids) &&
      alltrue([
        for issuer_id in var.issuer_ids : can(regex("^[0-9]{1,10}$", issuer_id))
      ])
    )
    error_message = "issuer_ids must contain 1-5 unique SEC CIKs of at most 10 digits."
  }
}

variable "filing_forms" {
  description = "Filing forms included in scheduled discovery."
  type        = list(string)
  default     = ["10-K", "10-Q"]

  validation {
    condition = (
      length(var.filing_forms) > 0 &&
      length(distinct(var.filing_forms)) == length(var.filing_forms) &&
      alltrue([
        for form in var.filing_forms : contains(["10-K", "10-Q"], form)
      ])
    )
    error_message = "filing_forms must contain unique values selected from 10-K and 10-Q."
  }
}

variable "lookback_days" {
  description = "Number of prior days included in each scheduled discovery window."
  type        = number
  default     = 7

  validation {
    condition = (
      var.lookback_days >= 1 &&
      var.lookback_days <= 30 &&
      floor(var.lookback_days) == var.lookback_days
    )
    error_message = "lookback_days must be an integer from 1 through 30."
  }
}

variable "schedule_expression" {
  description = "EventBridge Scheduler rate or cron expression."
  type        = string
  default     = "rate(6 hours)"

  validation {
    condition = (
      startswith(var.schedule_expression, "rate(") ||
      startswith(var.schedule_expression, "cron(")
    )
    error_message = "schedule_expression must be an EventBridge rate(...) or cron(...) expression."
  }
}

variable "schedule_enabled" {
  description = "Whether recurring discovery executions are enabled after deployment."
  type        = bool
  default     = false
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention for Lambda and Step Functions logs."
  type        = number
  default     = 14

  validation {
    condition = contains(
      [1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365],
      var.log_retention_days,
    )
    error_message = "log_retention_days must be a supported CloudWatch retention value up to 365 days."
  }
}
