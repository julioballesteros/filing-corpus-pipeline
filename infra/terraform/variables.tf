variable "aws_region" {
  description = "AWS Region in which to deploy the ingestion resources."
  type        = string
  default     = "eu-west-1"
}

variable "allowed_account_ids" {
  description = "AWS account IDs in which Terraform is permitted to operate."
  type        = set(string)

  validation {
    condition = (
      length(var.allowed_account_ids) > 0 &&
      alltrue([
        for account_id in var.allowed_account_ids :
        can(regex("^[0-9]{12}$", account_id))
      ])
    )
    error_message = "allowed_account_ids must contain at least one 12-digit AWS account ID."
  }
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

variable "registry_deletion_protection_enabled" {
  description = "Protect the filing registry from Terraform and API deletion."
  type        = bool
  default     = false
}

variable "raw_bucket_force_destroy" {
  description = "Allow Terraform to delete all raw object versions during bucket destruction."
  type        = bool
  default     = false
}

variable "normalized_bucket_force_destroy" {
  description = "Allow Terraform to delete all normalized corpus versions during bucket destruction."
  type        = bool
  default     = false
}

variable "target_config_bucket_force_destroy" {
  description = "Allow Terraform to delete all discovery-target object versions during bucket destruction."
  type        = bool
  default     = false
}

variable "acquisition_map_max_concurrency" {
  description = "Maximum filing acquisitions executed concurrently by Step Functions."
  type        = number
  default     = 2

  validation {
    condition = (
      var.acquisition_map_max_concurrency >= 1 &&
      var.acquisition_map_max_concurrency <= 5 &&
      floor(var.acquisition_map_max_concurrency) == var.acquisition_map_max_concurrency
    )
    error_message = "acquisition_map_max_concurrency must be an integer from 1 through 5."
  }
}

variable "acquisition_lease_seconds" {
  description = "Registry claim lease assigned to each acquisition invocation."
  type        = number
  default     = 300

  validation {
    condition = (
      var.acquisition_lease_seconds >= 120 &&
      var.acquisition_lease_seconds <= 3600 &&
      floor(var.acquisition_lease_seconds) == var.acquisition_lease_seconds
    )
    error_message = "acquisition_lease_seconds must be an integer from 120 through 3600."
  }
}

variable "acquisition_max_document_bytes" {
  description = "Maximum source-document response size accepted by acquisition."
  type        = number
  default     = 26214400

  validation {
    condition = (
      var.acquisition_max_document_bytes >= 1048576 &&
      var.acquisition_max_document_bytes <= 52428800 &&
      floor(var.acquisition_max_document_bytes) == var.acquisition_max_document_bytes
    )
    error_message = "acquisition_max_document_bytes must be an integer from 1 MiB through 50 MiB."
  }
}

variable "acquisition_max_filing_detail_bytes" {
  description = "Maximum SEC filing-detail response size accepted during acquisition."
  type        = number
  default     = 2097152

  validation {
    condition = (
      var.acquisition_max_filing_detail_bytes >= 65536 &&
      var.acquisition_max_filing_detail_bytes <= 10485760 &&
      floor(var.acquisition_max_filing_detail_bytes) == var.acquisition_max_filing_detail_bytes
    )
    error_message = "acquisition_max_filing_detail_bytes must be an integer from 64 KiB through 10 MiB."
  }
}

variable "normalization_lease_seconds" {
  description = "Registry claim lease assigned to each normalization invocation."
  type        = number
  default     = 600

  validation {
    condition = (
      var.normalization_lease_seconds >= 180 &&
      var.normalization_lease_seconds <= 3600 &&
      floor(var.normalization_lease_seconds) == var.normalization_lease_seconds
    )
    error_message = "normalization_lease_seconds must be an integer from 180 through 3600."
  }
}

variable "normalization_max_document_bytes" {
  description = "Maximum raw filing size loaded and parsed by normalization."
  type        = number
  default     = 26214400

  validation {
    condition = (
      var.normalization_max_document_bytes >= 1048576 &&
      var.normalization_max_document_bytes <= 52428800 &&
      floor(var.normalization_max_document_bytes) == var.normalization_max_document_bytes &&
      var.normalization_max_document_bytes <= var.acquisition_max_document_bytes
    )
    error_message = "normalization_max_document_bytes must be 1-50 MiB and no larger than the acquisition limit."
  }
}

variable "discovery_lambda_reserved_concurrency" {
  description = "Optional discovery Lambda concurrency reservation; null uses regional unreserved concurrency."
  type        = number
  default     = null
  nullable    = true

  validation {
    condition = (
      var.discovery_lambda_reserved_concurrency == null ||
      (
        var.discovery_lambda_reserved_concurrency >= 1 &&
        var.discovery_lambda_reserved_concurrency <= 10 &&
        floor(var.discovery_lambda_reserved_concurrency) == var.discovery_lambda_reserved_concurrency
      )
    )
    error_message = "discovery_lambda_reserved_concurrency must be null or an integer from 1 through 10."
  }
}
