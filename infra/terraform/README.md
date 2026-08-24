# Discovery infrastructure

This Terraform stack deploys the first asynchronous ingestion slice:

```text
EventBridge Scheduler -> Standard Step Functions -> discovery Lambda -> SEC
```

The state machine is the execution record and retry boundary. It invokes the
Lambda once and returns only the Lambda payload, so later map/acquisition states
can consume the discovered filing references without knowing the Lambda service
response envelope.

## Included

- a dependency-free Python 3.13 Lambda ZIP built from `src/`;
- a Standard Step Functions state machine with transient Lambda retries;
- an EventBridge schedule with a deterministic rolling-window input;
- least-privilege execution roles for the three services;
- retained JSON Lambda logs, complete workflow logs, and X-Ray tracing; and
- a five-company watchlist limit and reserved Lambda concurrency of one to
  bound execution time and SEC request pressure.

No database, queue, object store, downloader, or document processor is created
in this slice. Terraform state is local for now; a remote backend should be
bootstrapped before multiple people or automated deployment share this stack.

## Deploy

Prerequisites are Terraform 1.14+, AWS credentials for the target account, and
permission to manage Lambda, IAM, Step Functions, EventBridge Scheduler,
CloudWatch Logs, and X-Ray configuration.

```bash
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars
# Replace the example AWS account ID and SEC contact address before continuing.
terraform init
terraform plan -out=discovery.tfplan
terraform apply discovery.tfplan
```

The AWS provider checks `allowed_account_ids` before planning or applying, so
credentials for an unexpected account fail closed instead of creating a second
copy of the stack there.

The schedule is disabled by default. This prevents an apply from immediately
making external requests and gives you a chance to verify a manual execution.

## Verify manually

Use exact filing dates for a replayable smoke test:

```bash
STATE_MACHINE_ARN="$(terraform output -raw state_machine_arn)"
aws stepfunctions start-execution \
  --state-machine-arn "$STATE_MACHINE_ARN" \
  --input '{"provider":"sec","issuer_ids":["0000320193"],"forms":["10-K","10-Q"],"filed_from":"2025-01-01","filed_to":"2025-12-31"}'
```

Inspect the execution in Step Functions and confirm its output contains
`issuers_scanned` and `filings`. Lambda logs are under
`/aws/lambda/filing-corpus-pipeline-dev-discovery`; workflow logs are under
`/aws/vendedlogs/states/filing-corpus-pipeline-dev-discovery`.

Once the smoke test succeeds, set `schedule_enabled = true`, review another
plan, and apply it. EventBridge replaces `<aws.scheduler.scheduled-time>` for
each invocation; the Lambda derives and logs the inclusive discovery window.

## Reconfigure or remove

Change the watchlist, forms, lookback, frequency, or log retention through
variables, then plan before applying. To stop recurring work without deleting
resources, set `schedule_enabled = false` and apply. To remove the whole slice:

```bash
terraform destroy
```
