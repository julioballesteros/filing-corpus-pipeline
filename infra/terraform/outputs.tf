output "discovery_lambda_function_name" {
  description = "Deployed discovery Lambda function name."
  value       = aws_lambda_function.discovery.function_name
}

output "discovery_lambda_function_arn" {
  description = "Deployed discovery Lambda function ARN."
  value       = aws_lambda_function.discovery.arn
}

output "acquisition_lambda_function_name" {
  description = "Deployed acquisition Lambda function name."
  value       = aws_lambda_function.acquisition.function_name
}

output "acquisition_lambda_function_arn" {
  description = "Deployed acquisition Lambda function ARN."
  value       = aws_lambda_function.acquisition.arn
}

output "normalization_lambda_function_name" {
  description = "Deployed normalization Lambda function name."
  value       = aws_lambda_function.normalization.function_name
}

output "normalization_lambda_function_arn" {
  description = "Deployed normalization Lambda function ARN."
  value       = aws_lambda_function.normalization.arn
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

output "discovery_target_config" {
  description = "Exact deployed discovery-target object identity."
  value = {
    bucket     = aws_s3_bucket.target_config.bucket
    key        = aws_s3_object.discovery_targets.key
    version_id = aws_s3_object.discovery_targets.version_id
    sha256     = local.discovery_target_manifest_sha256
  }
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

output "normalized_corpus_bucket_name" {
  description = "Private S3 bucket containing normalized corpus artifacts."
  value       = aws_s3_bucket.normalized_corpus.bucket
}

output "normalized_corpus_bucket_arn" {
  description = "ARN of the normalized filing corpus bucket."
  value       = aws_s3_bucket.normalized_corpus.arn
}
