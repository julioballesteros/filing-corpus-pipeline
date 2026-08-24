data "archive_file" "discovery" {
  type        = "zip"
  source_dir  = "${path.module}/../../src"
  output_path = "${path.module}/discovery-lambda.zip"

  excludes = [
    "**/__pycache__/**",
    "**/*.pyc",
  ]

  output_file_mode = "0644"
}

resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${local.lambda_function_name}"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "discovery" {
  function_name = local.lambda_function_name
  description   = "Discovers new SEC 10-K and 10-Q filing references."
  role          = aws_iam_role.lambda.arn

  filename         = data.archive_file.discovery.output_path
  source_code_hash = data.archive_file.discovery.output_base64sha256
  handler          = "filing_corpus_pipeline.entrypoints.lambda_handler.handler"
  runtime          = "python3.13"
  architectures    = ["arm64"]

  memory_size                    = 512
  timeout                        = 180
  reserved_concurrent_executions = 1

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
    aws_cloudwatch_log_group.lambda,
    aws_iam_role_policy.lambda_runtime,
  ]
}
