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
