data "aws_iam_policy_document" "lambda_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "discovery_lambda" {
  name               = "${local.name_prefix}-discovery-lambda"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

data "aws_iam_policy_document" "discovery_lambda_runtime" {
  statement {
    sid = "WriteFunctionLogs"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.discovery_lambda.arn}:*"]
  }

  statement {
    sid = "PublishXRayTelemetry"
    actions = [
      "xray:PutTelemetryRecords",
      "xray:PutTraceSegments",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "discovery_lambda_runtime" {
  name   = "runtime"
  role   = aws_iam_role.discovery_lambda.id
  policy = data.aws_iam_policy_document.discovery_lambda_runtime.json
}

resource "aws_iam_role" "acquisition_lambda" {
  name               = "${local.name_prefix}-acquisition-lambda"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

data "aws_iam_policy_document" "acquisition_lambda_runtime" {
  statement {
    sid = "WriteFunctionLogs"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.acquisition_lambda.arn}:*"]
  }

  statement {
    sid = "UpdateFilingRegistry"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:UpdateItem",
    ]
    resources = [aws_dynamodb_table.filing_registry.arn]
  }

  statement {
    sid = "StoreRawDocuments"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
    ]
    resources = ["${aws_s3_bucket.raw_documents.arn}/raw/*"]
  }

  statement {
    sid = "PublishXRayTelemetry"
    actions = [
      "xray:PutTelemetryRecords",
      "xray:PutTraceSegments",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "acquisition_lambda_runtime" {
  name   = "runtime"
  role   = aws_iam_role.acquisition_lambda.id
  policy = data.aws_iam_policy_document.acquisition_lambda_runtime.json
}

resource "aws_iam_role" "normalization_lambda" {
  name               = "${local.name_prefix}-normalization-lambda"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

data "aws_iam_policy_document" "normalization_lambda_runtime" {
  statement {
    sid = "WriteFunctionLogs"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.normalization_lambda.arn}:*"]
  }

  statement {
    sid = "UpdateFilingRegistry"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:UpdateItem",
    ]
    resources = [aws_dynamodb_table.filing_registry.arn]
  }

  statement {
    sid = "ReadRawDocuments"
    actions = [
      "s3:GetObject",
      "s3:GetObjectVersion",
    ]
    resources = ["${aws_s3_bucket.raw_documents.arn}/raw/*"]
  }

  statement {
    sid = "PublishNormalizedCorpus"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
    ]
    resources = ["${aws_s3_bucket.normalized_corpus.arn}/normalized/*"]
  }

  statement {
    sid = "PublishXRayTelemetry"
    actions = [
      "xray:PutTelemetryRecords",
      "xray:PutTraceSegments",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "normalization_lambda_runtime" {
  name   = "runtime"
  role   = aws_iam_role.normalization_lambda.id
  policy = data.aws_iam_policy_document.normalization_lambda_runtime.json
}

data "aws_iam_policy_document" "step_functions_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["states.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "step_functions" {
  name               = "${local.name_prefix}-discovery-workflow"
  assume_role_policy = data.aws_iam_policy_document.step_functions_assume_role.json
}

data "aws_iam_policy_document" "step_functions" {
  statement {
    sid     = "InvokeIngestionFunctions"
    actions = ["lambda:InvokeFunction"]
    resources = [
      aws_lambda_function.acquisition.arn,
      aws_lambda_function.discovery.arn,
      aws_lambda_function.normalization.arn,
    ]
  }

  statement {
    sid = "DeliverExecutionLogs"
    actions = [
      "logs:CreateLogDelivery",
      "logs:DeleteLogDelivery",
      "logs:DescribeLogGroups",
      "logs:DescribeResourcePolicies",
      "logs:GetLogDelivery",
      "logs:ListLogDeliveries",
      "logs:PutResourcePolicy",
      "logs:UpdateLogDelivery",
    ]
    resources = ["*"]
  }

  statement {
    sid = "PublishXRayTelemetry"
    actions = [
      "xray:GetSamplingRules",
      "xray:GetSamplingTargets",
      "xray:PutTelemetryRecords",
      "xray:PutTraceSegments",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "step_functions" {
  name   = "workflow"
  role   = aws_iam_role.step_functions.id
  policy = data.aws_iam_policy_document.step_functions.json
}

data "aws_iam_policy_document" "scheduler_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${local.name_prefix}-discovery-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume_role.json
}

data "aws_iam_policy_document" "scheduler" {
  statement {
    sid       = "StartDiscoveryWorkflow"
    actions   = ["states:StartExecution"]
    resources = [aws_sfn_state_machine.discovery.arn]
  }
}

resource "aws_iam_role_policy" "scheduler" {
  name   = "start-workflow"
  role   = aws_iam_role.scheduler.id
  policy = data.aws_iam_policy_document.scheduler.json
}
