locals {
  name_prefix = "${var.project_name}-${var.environment}"

  discovery_lambda_function_name     = "${local.name_prefix}-discovery"
  acquisition_lambda_function_name   = "${local.name_prefix}-acquisition"
  normalization_lambda_function_name = "${local.name_prefix}-normalization"
  state_machine_name                 = "${local.name_prefix}-discovery"
  schedule_name                      = "${local.name_prefix}-discovery"
  schedule_group_name                = local.name_prefix
  registry_table_name                = "${local.name_prefix}-filing-registry"
  raw_bucket_name = format(
    "%s-raw-%s-%s",
    substr(local.name_prefix, 0, 30),
    data.aws_caller_identity.current.account_id,
    substr(sha256("${local.name_prefix}:${var.aws_region}"), 0, 10),
  )
  normalized_bucket_name = format(
    "%s-normalized-%s-%s",
    substr(local.name_prefix, 0, 28),
    data.aws_caller_identity.current.account_id,
    substr(sha256("${local.name_prefix}:${var.aws_region}:normalized"), 0, 10),
  )
  lambda_package_directory = abspath("${path.module}/../../build/lambda")
  discovery_lambda_package_path = (
    "${local.lambda_package_directory}/discovery-lambda.zip"
  )
  acquisition_lambda_package_path = (
    "${local.lambda_package_directory}/acquisition-lambda.zip"
  )
  normalization_lambda_package_path = (
    "${local.lambda_package_directory}/normalization-lambda.zip"
  )

  common_tags = {
    Environment = var.environment
    ManagedBy   = "Terraform"
    Project     = var.project_name
    Service     = "filing-ingestion"
  }
}
