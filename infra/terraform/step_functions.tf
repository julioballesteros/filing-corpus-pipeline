resource "aws_cloudwatch_log_group" "step_functions" {
  name              = "/aws/vendedlogs/states/${local.state_machine_name}"
  retention_in_days = var.log_retention_days
}

resource "aws_sfn_state_machine" "discovery" {
  name     = local.state_machine_name
  role_arn = aws_iam_role.step_functions.arn
  type     = "STANDARD"

  definition = jsonencode({
    Comment = "Discover, acquire, and normalize configured filings for one deterministic date window."
    StartAt = "DiscoverFilings"
    States = {
      DiscoverFilings = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = aws_lambda_function.discovery.arn
          Payload = {
            "window.$" = "$"
            target_config = {
              bucket     = aws_s3_bucket.target_config.bucket
              key        = aws_s3_object.discovery_targets.key
              version_id = aws_s3_object.discovery_targets.version_id
              sha256     = local.discovery_target_manifest_sha256
            }
          }
        }
        OutputPath = "$.Payload"
        Retry = [
          {
            ErrorEquals     = ["RetryableDiscoveryTargetError"]
            IntervalSeconds = 2
            MaxAttempts     = 3
            BackoffRate     = 2
          },
          {
            ErrorEquals = [
              "Lambda.AWSLambdaException",
              "Lambda.SdkClientException",
              "Lambda.ServiceException",
              "Lambda.TooManyRequestsException",
            ]
            IntervalSeconds = 2
            MaxAttempts     = 4
            BackoffRate     = 2
          },
        ]
        Next = "AcquireFilings"
      }
      AcquireFilings = {
        Type           = "Map"
        ItemsPath      = "$.filings"
        MaxConcurrency = var.acquisition_map_max_concurrency
        ItemProcessor = {
          ProcessorConfig = {
            Mode = "INLINE"
          }
          StartAt = "PrepareFiling"
          States = {
            PrepareFiling = {
              Type = "Pass"
              Parameters = {
                "filing.$" = "$"
              }
              Next = "AcquireFiling"
            }
            AcquireFiling = {
              Type     = "Task"
              Resource = "arn:aws:states:::lambda:invoke"
              Parameters = {
                FunctionName = aws_lambda_function.acquisition.arn
                Payload = {
                  "filing.$"       = "$.filing"
                  "owner_id.$"     = "$$.Execution.Id"
                  "requested_at.$" = "$$.State.EnteredTime"
                }
              }
              ResultSelector = {
                "result.$" = "$.Payload"
              }
              ResultPath = "$.acquisition"
              Retry = [
                {
                  ErrorEquals = [
                    "RetryableAcquisitionError",
                    "FilingRegistryError",
                    "RegistryConsistencyError",
                    "RegistryLeaseLostError",
                  ]
                  IntervalSeconds = 2
                  MaxAttempts     = 3
                  BackoffRate     = 2
                },
                {
                  ErrorEquals = [
                    "Lambda.AWSLambdaException",
                    "Lambda.SdkClientException",
                    "Lambda.ServiceException",
                    "Lambda.TooManyRequestsException",
                  ]
                  IntervalSeconds = 2
                  MaxAttempts     = 4
                  BackoffRate     = 2
                },
              ]
              Catch = [
                {
                  ErrorEquals = ["States.ALL"]
                  ResultPath  = "$.acquisition_error"
                  Next        = "AcquisitionFailed"
                },
              ]
              Next = "RouteAcquisition"
            }
            RouteAcquisition = {
              Type = "Choice"
              Choices = [
                {
                  Variable     = "$.acquisition.result.outcome"
                  StringEquals = "RAW_STORED"
                  Next         = "NormalizeFiling"
                },
                {
                  Variable     = "$.acquisition.result.outcome"
                  StringEquals = "ALREADY_COMPLETED"
                  Next         = "NormalizeFiling"
                },
              ]
              Default = "AcquisitionDeferred"
            }
            NormalizeFiling = {
              Type     = "Task"
              Resource = "arn:aws:states:::lambda:invoke"
              Parameters = {
                FunctionName = aws_lambda_function.normalization.arn
                Payload = {
                  "filing.$"       = "$.filing"
                  "owner_id.$"     = "$$.Execution.Id"
                  "requested_at.$" = "$$.State.EnteredTime"
                }
              }
              ResultSelector = {
                "result.$" = "$.Payload"
              }
              ResultPath = "$.normalization"
              Retry = [
                {
                  ErrorEquals = [
                    "RetryableNormalizationError",
                    "FilingRegistryError",
                    "RegistryConsistencyError",
                    "RegistryLeaseLostError",
                  ]
                  IntervalSeconds = 2
                  MaxAttempts     = 3
                  BackoffRate     = 2
                },
                {
                  ErrorEquals = [
                    "Lambda.AWSLambdaException",
                    "Lambda.SdkClientException",
                    "Lambda.ServiceException",
                    "Lambda.TooManyRequestsException",
                  ]
                  IntervalSeconds = 2
                  MaxAttempts     = 4
                  BackoffRate     = 2
                },
              ]
              Catch = [
                {
                  ErrorEquals = ["States.ALL"]
                  ResultPath  = "$.normalization_error"
                  Next        = "NormalizationFailed"
                },
              ]
              Next = "BuildFilingResult"
            }
            BuildFilingResult = {
              Type = "Pass"
              Parameters = {
                "company_id.$"         = "$.filing.company_id"
                "document_policy.$"    = "$.filing.document_policy"
                "filing_type.$"        = "$.filing.filing_type"
                "provider.$"           = "$.filing.provider"
                "provider_filing_id.$" = "$.filing.provider_filing_id"
                "acquisition.$"        = "$.acquisition.result"
                "normalization.$"      = "$.normalization.result"
              }
              End = true
            }
            AcquisitionDeferred = {
              Type = "Pass"
              Parameters = {
                "company_id.$"         = "$.filing.company_id"
                "document_policy.$"    = "$.filing.document_policy"
                "filing_type.$"        = "$.filing.filing_type"
                "provider.$"           = "$.filing.provider"
                "provider_filing_id.$" = "$.filing.provider_filing_id"
                "acquisition.$"        = "$.acquisition.result"
                normalization          = null
              }
              End = true
            }
            AcquisitionFailed = {
              Type = "Pass"
              Parameters = {
                "company_id.$"         = "$.filing.company_id"
                "document_policy.$"    = "$.filing.document_policy"
                "filing_type.$"        = "$.filing.filing_type"
                "provider.$"           = "$.filing.provider"
                "provider_filing_id.$" = "$.filing.provider_filing_id"
                acquisition = {
                  outcome   = "FAILED"
                  "error.$" = "$.acquisition_error.Error"
                }
                normalization = null
              }
              End = true
            }
            NormalizationFailed = {
              Type = "Pass"
              Parameters = {
                "company_id.$"         = "$.filing.company_id"
                "document_policy.$"    = "$.filing.document_policy"
                "filing_type.$"        = "$.filing.filing_type"
                "provider.$"           = "$.filing.provider"
                "provider_filing_id.$" = "$.filing.provider_filing_id"
                "acquisition.$"        = "$.acquisition.result"
                normalization = {
                  outcome   = "FAILED"
                  "error.$" = "$.normalization_error.Error"
                }
              }
              End = true
            }
          }
        }
        ResultPath = "$.filing_results"
        Next       = "BuildIngestionResult"
      }
      BuildIngestionResult = {
        Type = "Pass"
        Parameters = {
          "issuers_scanned.$" = "$.issuers_scanned"
          "filings_found.$"   = "$.filings_found"
          "filing_results.$"  = "$.filing_results"
          "target_set.$"      = "$.target_set"
        }
        End = true
      }
    }
  })

  logging_configuration {
    include_execution_data = true
    level                  = "ALL"
    log_destination        = "${aws_cloudwatch_log_group.step_functions.arn}:*"
  }

  tracing_configuration {
    enabled = true
  }

  depends_on = [aws_iam_role_policy.step_functions]
}
