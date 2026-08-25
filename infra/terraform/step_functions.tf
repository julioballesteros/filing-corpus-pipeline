resource "aws_cloudwatch_log_group" "step_functions" {
  name              = "/aws/vendedlogs/states/${local.state_machine_name}"
  retention_in_days = var.log_retention_days
}

resource "aws_sfn_state_machine" "discovery" {
  name     = local.state_machine_name
  role_arn = aws_iam_role.step_functions.arn
  type     = "STANDARD"

  definition = jsonencode({
    Comment = "Discover and acquire SEC filings for one deterministic date window."
    StartAt = "DiscoverFilings"
    States = {
      DiscoverFilings = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = aws_lambda_function.discovery.arn
          "Payload.$"  = "$"
        }
        OutputPath = "$.Payload"
        Retry = [
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
          StartAt = "AcquireFiling"
          States = {
            AcquireFiling = {
              Type     = "Task"
              Resource = "arn:aws:states:::lambda:invoke"
              Parameters = {
                FunctionName = aws_lambda_function.acquisition.arn
                Payload = {
                  "filing.$"       = "$"
                  "owner_id.$"     = "$$.Execution.Id"
                  "requested_at.$" = "$$.State.EnteredTime"
                }
              }
              OutputPath = "$.Payload"
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
                  ResultPath  = "$.error"
                  Next        = "AcquisitionFailed"
                },
              ]
              End = true
            }
            AcquisitionFailed = {
              Type = "Pass"
              Parameters = {
                outcome                = "FAILED"
                "provider.$"           = "$.provider"
                "provider_filing_id.$" = "$.provider_filing_id"
                "error.$"              = "$.error.Error"
              }
              End = true
            }
          }
        }
        ResultPath = "$.acquisitions"
        Next       = "BuildIngestionResult"
      }
      BuildIngestionResult = {
        Type = "Pass"
        Parameters = {
          "issuers_scanned.$" = "$.issuers_scanned"
          "filings_found.$"   = "$.filings_found"
          "acquisitions.$"    = "$.acquisitions"
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
