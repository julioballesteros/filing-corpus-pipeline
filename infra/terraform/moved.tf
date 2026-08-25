moved {
  from = aws_cloudwatch_log_group.lambda
  to   = aws_cloudwatch_log_group.discovery_lambda
}

moved {
  from = aws_iam_role.lambda
  to   = aws_iam_role.discovery_lambda
}

moved {
  from = aws_iam_role_policy.lambda_runtime
  to   = aws_iam_role_policy.discovery_lambda_runtime
}
