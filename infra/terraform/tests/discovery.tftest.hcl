mock_provider "aws" {
  override_resource {
    target          = aws_lambda_function.discovery
    override_during = plan
    values = {
      arn = "arn:aws:lambda:eu-west-1:123456789012:function:filing-corpus-pipeline-dev-discovery"
    }
  }

  override_resource {
    target          = aws_lambda_function.acquisition
    override_during = plan
    values = {
      arn = "arn:aws:lambda:eu-west-1:123456789012:function:filing-corpus-pipeline-dev-acquisition"
    }
  }

  override_resource {
    target          = aws_lambda_function.normalization
    override_during = plan
    values = {
      arn = "arn:aws:lambda:eu-west-1:123456789012:function:filing-corpus-pipeline-dev-normalization"
    }
  }

  override_resource {
    target          = aws_s3_object.discovery_targets
    override_during = plan
    values = {
      version_id = "version-1"
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
    target = data.aws_iam_policy_document.discovery_lambda_runtime
    values = {
      json = "{\"Statement\":[],\"Version\":\"2012-10-17\"}"
    }
  }

  override_data {
    target = data.aws_iam_policy_document.acquisition_lambda_runtime
    values = {
      json = "{\"Statement\":[],\"Version\":\"2012-10-17\"}"
    }
  }

  override_data {
    target = data.aws_iam_policy_document.normalization_lambda_runtime
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
    target = data.aws_iam_policy_document.target_config
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


  override_data {
    target = data.aws_iam_policy_document.normalized_corpus
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
      "filing_corpus_pipeline.discovery.handler.handler"
    )
    error_message = "The Lambda must use the discovery handler."
  }

  assert {
    condition = (
      aws_lambda_function.acquisition.handler ==
      "filing_corpus_pipeline.acquisition.handler.handler"
    )
    error_message = "The acquisition Lambda must have its own explicit handler."
  }

  assert {
    condition = (
      aws_lambda_function.normalization.handler ==
      "filing_corpus_pipeline.normalization.handler.handler"
    )
    error_message = "The normalization Lambda must have its own explicit handler."
  }

  assert {
    condition = (
      aws_lambda_function.normalization.architectures == tolist(["arm64"]) &&
      aws_lambda_function.normalization.memory_size == 1024 &&
      aws_lambda_function.normalization.timeout == 180
    )
    error_message = "Normalization must use the architecture and bounded resources of its dependency ZIP."
  }

  assert {
    condition = (
      aws_lambda_function.normalization.environment[0].variables.NORMALIZATION_MAX_DOCUMENT_BYTES ==
      "26214400"
    )
    error_message = "The normalization Lambda must receive its raw-document size bound."
  }

  assert {
    condition     = aws_lambda_function.acquisition.timeout == 90
    error_message = "The acquisition Lambda must have a bounded execution timeout."
  }

  assert {
    condition = (
      aws_lambda_function.acquisition.environment[0].variables.MAX_DOCUMENT_BYTES ==
      "26214400"
    )
    error_message = "The acquisition Lambda must receive its document-size bound."
  }

  assert {
    condition     = aws_lambda_function.discovery.reserved_concurrent_executions == null
    error_message = "Reserved concurrency must be opt-in for quota-constrained accounts."
  }

  assert {
    condition = (
      jsondecode(aws_sfn_state_machine.discovery.definition)
      .States.DiscoverFilings.Next == "AcquireFilings"
    )
    error_message = "Discovery must hand its filing list to acquisition."
  }

  assert {
    condition = (
      jsondecode(aws_sfn_state_machine.discovery.definition)
      .States.DiscoverFilings.Parameters.Payload.target_config.version_id == "version-1" &&
      jsondecode(aws_sfn_state_machine.discovery.definition)
      .States.DiscoverFilings.Parameters.Payload.target_config.sha256 == filesha256("${path.module}/../../config/discovery-targets/dev.json") &&
      jsondecode(aws_sfn_state_machine.discovery.definition)
      .States.DiscoverFilings.Parameters.Payload["window.$"] == "$"
    )
    error_message = "The workflow must pin target bytes separately from the execution window."
  }

  assert {
    condition = (
      jsondecode(aws_sfn_state_machine.discovery.definition)
      .States.AcquireFilings.Type == "Map" &&
      jsondecode(aws_sfn_state_machine.discovery.definition)
      .States.AcquireFilings.MaxConcurrency == 2
    )
    error_message = "Acquisition must use the bounded Map concurrency."
  }

  assert {
    condition = (
      jsondecode(aws_sfn_state_machine.discovery.definition)
      .States.AcquireFilings.ItemProcessor.States.AcquireFiling
      .Parameters.Payload["owner_id.$"] == "$$.Execution.Id"
    )
    error_message = "Each acquisition claim must use the workflow execution ID."
  }

  assert {
    condition = (
      jsondecode(aws_sfn_state_machine.discovery.definition)
      .States.AcquireFilings.ItemProcessor.States.RouteAcquisition
      .Choices[0].Next == "NormalizeFiling" &&
      jsondecode(aws_sfn_state_machine.discovery.definition)
      .States.AcquireFilings.ItemProcessor.States.NormalizeFiling
      .Parameters.Payload["filing.$"] == "$.filing"
    )
    error_message = "Stored raw filings must flow to normalization without carrying document bytes."
  }

  assert {
    condition = contains(
      jsondecode(aws_sfn_state_machine.discovery.definition)
      .States.AcquireFilings.ItemProcessor.States.NormalizeFiling.Retry[0].ErrorEquals,
      "RetryableNormalizationError",
    )
    error_message = "The workflow must retry normalization failures classified as transient."
  }

  assert {
    condition = contains(
      jsondecode(aws_sfn_state_machine.discovery.definition)
      .States.AcquireFilings.ItemProcessor.States.AcquireFiling.Retry[0].ErrorEquals,
      "RetryableAcquisitionError",
    )
    error_message = "The workflow must retry failures classified as transient."
  }

  assert {
    condition = (
      jsondecode(aws_sfn_state_machine.discovery.definition)
      .States.AcquireFilings.ItemProcessor.States.AcquisitionFailed
      .Parameters.acquisition.outcome == "FAILED"
    )
    error_message = "One failed filing must become an isolated Map result."
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
    condition = (
      !contains(keys(jsondecode(aws_scheduler_schedule.discovery.target[0].input)), "issuer_ids") &&
      !contains(keys(jsondecode(aws_scheduler_schedule.discovery.target[0].input)), "forms")
    )
    error_message = "The scheduler must not own discovery companies or filing types."
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
    condition = alltrue([
      aws_s3_bucket_public_access_block.target_config.block_public_acls,
      aws_s3_bucket_public_access_block.target_config.block_public_policy,
      aws_s3_bucket_public_access_block.target_config.ignore_public_acls,
      aws_s3_bucket_public_access_block.target_config.restrict_public_buckets,
      aws_s3_bucket_versioning.target_config.versioning_configuration[0].status == "Enabled",
      aws_s3_bucket.target_config.force_destroy == false,
    ])
    error_message = "Discovery target configuration must be private, versioned, and retained by default."
  }

  assert {
    condition = (
      aws_s3_object.discovery_targets.content_type == "application/json" &&
      aws_s3_object.discovery_targets.metadata.sha256 == filesha256("${path.module}/../../config/discovery-targets/dev.json")
    )
    error_message = "The deployed target manifest must retain its content identity."
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

  assert {
    condition = startswith(
      aws_s3_bucket.normalized_corpus.bucket,
      "filing-corpus-pipeline-dev-normalized-123456789012-",
    )
    error_message = "The normalized bucket name must be stable and account-qualified."
  }

  assert {
    condition = alltrue([
      aws_s3_bucket_public_access_block.normalized_corpus.block_public_acls,
      aws_s3_bucket_public_access_block.normalized_corpus.block_public_policy,
      aws_s3_bucket_public_access_block.normalized_corpus.ignore_public_acls,
      aws_s3_bucket_public_access_block.normalized_corpus.restrict_public_buckets,
      aws_s3_bucket_versioning.normalized_corpus.versioning_configuration[0].status == "Enabled",
      aws_s3_bucket.normalized_corpus.force_destroy == false,
    ])
    error_message = "The normalized corpus bucket must be private, versioned, and retained by default."
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
    allowed_account_ids                   = ["123456789012"]
    discovery_lambda_reserved_concurrency = 1
    registry_deletion_protection_enabled  = true
    sec_user_agent                        = "filing-corpus-pipeline ci@example.com"
    schedule_enabled                      = true
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
