mock_provider "aws" {
  override_resource {
    target          = aws_lambda_function.discovery
    override_during = plan
    values = {
      arn = "arn:aws:lambda:eu-west-1:123456789012:function:filing-corpus-pipeline-dev-discovery"
    }
  }

  override_data {
    target          = data.aws_caller_identity.current
    override_during = plan
    values = {
      account_id = "123456789012"
    }
  }

  override_data {
    target = data.aws_iam_policy_document.lambda_assume_role
    values = {
      json = "{\"Statement\":[],\"Version\":\"2012-10-17\"}"
    }
  }

  override_data {
    target = data.aws_iam_policy_document.lambda_runtime
    values = {
      json = "{\"Statement\":[],\"Version\":\"2012-10-17\"}"
    }
  }

  override_data {
    target = data.aws_iam_policy_document.step_functions_assume_role
    values = {
      json = "{\"Statement\":[],\"Version\":\"2012-10-17\"}"
    }
  }

  override_data {
    target = data.aws_iam_policy_document.step_functions
    values = {
      json = "{\"Statement\":[],\"Version\":\"2012-10-17\"}"
    }
  }

  override_data {
    target = data.aws_iam_policy_document.scheduler_assume_role
    values = {
      json = "{\"Statement\":[],\"Version\":\"2012-10-17\"}"
    }
  }

  override_data {
    target = data.aws_iam_policy_document.scheduler
    values = {
      json = "{\"Statement\":[],\"Version\":\"2012-10-17\"}"
    }
  }

  override_data {
    target = data.aws_iam_policy_document.raw_documents
    values = {
      json = "{\"Statement\":[],\"Version\":\"2012-10-17\"}"
    }
  }
}

run "default_ingestion_slice" {
  command = plan

  variables {
    allowed_account_ids = ["123456789012"]
    lookback_days       = 7
    schedule_enabled    = false
    sec_user_agent      = "filing-corpus-pipeline ci@example.com"
  }

  assert {
    condition     = aws_scheduler_schedule.discovery.state == "DISABLED"
    error_message = "The recurring schedule must be disabled by default."
  }

  assert {
    condition = (
      aws_lambda_function.discovery.handler ==
      "filing_corpus_pipeline.entrypoints.lambda_handler.handler"
    )
    error_message = "The Lambda must use the discovery handler."
  }

  assert {
    condition     = aws_lambda_function.discovery.reserved_concurrent_executions == null
    error_message = "Reserved concurrency must be opt-in for quota-constrained accounts."
  }

  assert {
    condition     = aws_lambda_function.discovery.timeout == 180
    error_message = "The Lambda timeout must cover bounded SEC client retries."
  }

  assert {
    condition = (
      jsondecode(aws_sfn_state_machine.discovery.definition)
      .States.DiscoverFilings.OutputPath == "$.Payload"
    )
    error_message = "The workflow must return the discovery payload without the Lambda envelope."
  }

  assert {
    condition = (
      jsondecode(aws_scheduler_schedule.discovery.target[0].input)
      .scheduled_at == "<aws.scheduler.scheduled-time>"
    )
    error_message = "The schedule must pass its deterministic invocation time to Lambda."
  }

  assert {
    condition = (
      jsondecode(aws_scheduler_schedule.discovery.target[0].input)
      .lookback_days == 7
    )
    error_message = "The schedule must pass the configured lookback window."
  }

  assert {
    condition     = aws_dynamodb_table.filing_registry.billing_mode == "PAY_PER_REQUEST"
    error_message = "The portfolio-scale registry must avoid provisioned idle capacity."
  }

  assert {
    condition     = aws_dynamodb_table.filing_registry.hash_key == "filing_key"
    error_message = "The registry must use the provider-qualified filing identity."
  }

  assert {
    condition     = aws_dynamodb_table.filing_registry.point_in_time_recovery[0].enabled
    error_message = "The registry must have point-in-time recovery enabled."
  }

  assert {
    condition     = aws_dynamodb_table.filing_registry.server_side_encryption[0].enabled
    error_message = "The registry must be encrypted at rest."
  }

  assert {
    condition     = aws_dynamodb_table.filing_registry.deletion_protection_enabled == false
    error_message = "Deletion protection must remain opt-in for the disposable dev stack."
  }

  assert {
    condition = startswith(
      aws_s3_bucket.raw_documents.bucket,
      "filing-corpus-pipeline-dev-raw-123456789012-",
    )
    error_message = "The raw bucket name must be stable and account-qualified."
  }

  assert {
    condition     = aws_s3_bucket.raw_documents.force_destroy == false
    error_message = "Terraform must not delete a nonempty raw bucket by default."
  }

  assert {
    condition     = aws_s3_bucket_ownership_controls.raw_documents.rule[0].object_ownership == "BucketOwnerEnforced"
    error_message = "Raw objects must always be owned by the bucket account."
  }

  assert {
    condition = alltrue([
      aws_s3_bucket_public_access_block.raw_documents.block_public_acls,
      aws_s3_bucket_public_access_block.raw_documents.block_public_policy,
      aws_s3_bucket_public_access_block.raw_documents.ignore_public_acls,
      aws_s3_bucket_public_access_block.raw_documents.restrict_public_buckets,
    ])
    error_message = "Every S3 public-access control must be enabled."
  }

  assert {
    condition = (
      one(one(
        aws_s3_bucket_server_side_encryption_configuration.raw_documents.rule
      ).apply_server_side_encryption_by_default).sse_algorithm == "AES256"
    )
    error_message = "Raw filing objects must be encrypted at rest."
  }

  assert {
    condition     = aws_s3_bucket_versioning.raw_documents.versioning_configuration[0].status == "Enabled"
    error_message = "Raw filing objects must be versioned for overwrite recovery."
  }

  assert {
    condition = toset([
      for rule in aws_s3_bucket_lifecycle_configuration.raw_documents.rule : rule.id
      ]) == toset([
      "abort-incomplete-multipart-uploads",
      "expire-noncurrent-versions",
      "remove-expired-delete-markers",
    ])
    error_message = "The raw bucket must retain all cost and recovery lifecycle rules."
  }
}

run "invalid_account_allowlist" {
  command = plan

  variables {
    allowed_account_ids = ["not-an-account-id"]
    sec_user_agent      = "filing-corpus-pipeline ci@example.com"
  }

  expect_failures = [var.allowed_account_ids]
}

run "enabled_schedule" {
  command = plan

  variables {
    allowed_account_ids                  = ["123456789012"]
    lambda_reserved_concurrency          = 1
    registry_deletion_protection_enabled = true
    sec_user_agent                       = "filing-corpus-pipeline ci@example.com"
    schedule_enabled                     = true
  }

  assert {
    condition     = aws_scheduler_schedule.discovery.state == "ENABLED"
    error_message = "schedule_enabled must control the deployed schedule state."
  }

  assert {
    condition     = aws_lambda_function.discovery.reserved_concurrent_executions == 1
    error_message = "Reserved concurrency must be configurable when quota permits."
  }

  assert {
    condition     = aws_dynamodb_table.filing_registry.deletion_protection_enabled
    error_message = "Registry deletion protection must be configurable for long-lived stacks."
  }
}
