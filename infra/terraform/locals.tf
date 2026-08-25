locals {
  name_prefix = "${var.project_name}-${var.environment}"

  discovery_lambda_function_name   = "${local.name_prefix}-discovery"
  acquisition_lambda_function_name = "${local.name_prefix}-acquisition"
  state_machine_name               = "${local.name_prefix}-discovery"
  schedule_name                    = "${local.name_prefix}-discovery"
  schedule_group_name              = local.name_prefix
  registry_table_name              = "${local.name_prefix}-filing-registry"
  raw_bucket_name = format(
    "%s-raw-%s-%s",
    substr(local.name_prefix, 0, 30),
    data.aws_caller_identity.current.account_id,
    substr(sha256("${local.name_prefix}:${var.aws_region}"), 0, 10),
  )

  common_tags = {
    Environment = var.environment
    ManagedBy   = "Terraform"
    Project     = var.project_name
    Service     = "filing-ingestion"
  }
}
