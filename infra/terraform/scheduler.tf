resource "aws_scheduler_schedule_group" "discovery" {
  name = local.schedule_group_name
}

resource "aws_scheduler_schedule" "discovery" {
  name        = local.schedule_name
  group_name  = aws_scheduler_schedule_group.discovery.name
  description = "Starts recurring SEC filing discovery executions."
  state       = var.schedule_enabled ? "ENABLED" : "DISABLED"

  schedule_expression = var.schedule_expression

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_sfn_state_machine.discovery.arn
    role_arn = aws_iam_role.scheduler.arn
    input = templatefile("${path.module}/scheduler-input.json.tftpl", {
      lookback_days = var.lookback_days
    })

    retry_policy {
      maximum_event_age_in_seconds = 3600
      maximum_retry_attempts       = 3
    }
  }

  depends_on = [aws_iam_role_policy.scheduler]
}
