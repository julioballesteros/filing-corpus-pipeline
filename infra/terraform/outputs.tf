output "lambda_function_name" {
  description = "Deployed discovery Lambda function name."
  value       = aws_lambda_function.discovery.function_name
}

output "lambda_function_arn" {
  description = "Deployed discovery Lambda function ARN."
  value       = aws_lambda_function.discovery.arn
}

output "state_machine_arn" {
  description = "Discovery Step Functions state machine ARN."
  value       = aws_sfn_state_machine.discovery.arn
}

output "schedule_arn" {
  description = "Recurring discovery schedule ARN."
  value       = aws_scheduler_schedule.discovery.arn
}

output "schedule_state" {
  description = "Whether recurring discovery is currently enabled."
  value       = aws_scheduler_schedule.discovery.state
}

output "filing_registry_table_name" {
  description = "DynamoDB table used for atomic filing claims and acquisition state."
  value       = aws_dynamodb_table.filing_registry.name
}

output "filing_registry_table_arn" {
  description = "ARN of the DynamoDB filing registry table."
  value       = aws_dynamodb_table.filing_registry.arn
}

output "raw_documents_bucket_name" {
  description = "Private S3 bucket containing immutable-source filing documents."
  value       = aws_s3_bucket.raw_documents.bucket
}

output "raw_documents_bucket_arn" {
  description = "ARN of the raw filing document bucket."
  value       = aws_s3_bucket.raw_documents.arn
}
