resource "aws_cloudwatch_log_group" "discovery_lambda" {
  name              = "/aws/lambda/${local.discovery_lambda_function_name}"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "discovery" {
  function_name = local.discovery_lambda_function_name
  description   = "Discovers new SEC 10-K and 10-Q filing references."
  role          = aws_iam_role.discovery_lambda.arn

  filename         = local.discovery_lambda_package_path
  source_code_hash = filebase64sha256(local.discovery_lambda_package_path)
  handler          = "filing_corpus_pipeline.discovery.handler.handler"
  runtime          = "python3.13"
  architectures    = ["arm64"]

  memory_size                    = 512
  timeout                        = 180
  reserved_concurrent_executions = var.discovery_lambda_reserved_concurrency

  environment {
    variables = {
      SEC_USER_AGENT = var.sec_user_agent
    }
  }

  logging_config {
    application_log_level = "INFO"
    log_format            = "JSON"
    system_log_level      = "WARN"
  }

  tracing_config {
    mode = "Active"
  }

  depends_on = [
    aws_cloudwatch_log_group.discovery_lambda,
    aws_iam_role_policy.discovery_lambda_runtime,
  ]
}

resource "aws_cloudwatch_log_group" "acquisition_lambda" {
  name              = "/aws/lambda/${local.acquisition_lambda_function_name}"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "acquisition" {
  function_name = local.acquisition_lambda_function_name
  description   = "Claims and durably stores one discovered SEC filing."
  role          = aws_iam_role.acquisition_lambda.arn

  filename         = local.acquisition_lambda_package_path
  source_code_hash = filebase64sha256(local.acquisition_lambda_package_path)
  handler          = "filing_corpus_pipeline.acquisition.handler.handler"
  runtime          = "python3.13"
  architectures    = ["arm64"]

  memory_size = 512
  timeout     = 90

  environment {
    variables = {
      ACQUISITION_LEASE_SECONDS = tostring(var.acquisition_lease_seconds)
      MAX_DOCUMENT_BYTES        = tostring(var.acquisition_max_document_bytes)
      RAW_BUCKET_NAME           = aws_s3_bucket.raw_documents.bucket
      REGISTRY_TABLE_NAME       = aws_dynamodb_table.filing_registry.name
      SEC_USER_AGENT            = var.sec_user_agent
    }
  }

  logging_config {
    application_log_level = "INFO"
    log_format            = "JSON"
    system_log_level      = "WARN"
  }

  tracing_config {
    mode = "Active"
  }

  depends_on = [
    aws_cloudwatch_log_group.acquisition_lambda,
    aws_iam_role_policy.acquisition_lambda_runtime,
  ]
}

resource "aws_cloudwatch_log_group" "normalization_lambda" {
  name              = "/aws/lambda/${local.normalization_lambda_function_name}"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "normalization" {
  function_name = local.normalization_lambda_function_name
  description   = "Normalizes one raw SEC filing into a versioned corpus."
  role          = aws_iam_role.normalization_lambda.arn

  filename         = local.normalization_lambda_package_path
  source_code_hash = filebase64sha256(local.normalization_lambda_package_path)
  handler          = "filing_corpus_pipeline.normalization.handler.handler"
  runtime          = "python3.13"
  architectures    = ["arm64"]

  memory_size = 1024
  timeout     = 180

  environment {
    variables = {
      NORMALIZATION_LEASE_SECONDS      = tostring(var.normalization_lease_seconds)
      NORMALIZATION_MAX_DOCUMENT_BYTES = tostring(var.normalization_max_document_bytes)
      NORMALIZED_BUCKET_NAME           = aws_s3_bucket.normalized_corpus.bucket
      RAW_BUCKET_NAME                  = aws_s3_bucket.raw_documents.bucket
      REGISTRY_TABLE_NAME              = aws_dynamodb_table.filing_registry.name
    }
  }

  logging_config {
    application_log_level = "INFO"
    log_format            = "JSON"
    system_log_level      = "WARN"
  }

  tracing_config {
    mode = "Active"
  }

  depends_on = [
    aws_cloudwatch_log_group.normalization_lambda,
    aws_iam_role_policy.normalization_lambda_runtime,
  ]
}
