locals {
  name_prefix = "${var.project_name}-${var.environment}"

  lambda_function_name = "${local.name_prefix}-discovery"
  state_machine_name   = "${local.name_prefix}-discovery"
  schedule_name        = "${local.name_prefix}-discovery"
  schedule_group_name  = local.name_prefix
  registry_table_name  = "${local.name_prefix}-filing-registry"

  common_tags = {
    Environment = var.environment
    ManagedBy   = "Terraform"
    Project     = var.project_name
    Service     = "filing-discovery"
  }
}
