# Ingestion infrastructure

This Terraform stack deploys the first asynchronous ingestion slice:

```text
EventBridge Scheduler -> Standard Step Functions -> discovery Lambda -> SEC
                                            |
                                            +-> bounded filing Map
                                                  |
                                                  +-> acquisition Lambda
                                                  |     |-> SEC archive
                                                  |     |-> DynamoDB registry
                                                  |     +-> S3 raw bucket
                                                  |
                                                  +-> normalization Lambda
                                                        |-> S3 raw bucket
                                                        |-> DynamoDB registry
                                                        +-> S3 normalized bucket
```

The state machine is the execution record and retry boundary. Discovery returns
provider-neutral filing references. A bounded inline `Map` invokes acquisition
and then normalization for each ready filing, passing the workflow execution
ARN as both claim owners. Filing bodies never enter workflow state.

## Included

- separate Python 3.13 ZIPs and explicit handlers for discovery, acquisition,
  and normalization; all package the locked Linux arm64 Pydantic runtime, and
  normalization additionally packages Linux arm64 `lxml`;
- a Standard Step Functions state machine with a failure-isolated acquisition
  `Map` and classified retries;
- an EventBridge schedule with a deterministic rolling-window input;
- least-privilege roles, including item-level DynamoDB actions and distinct raw
  read/normalized write access for normalization;
- retained JSON Lambda logs, complete workflow logs, and X-Ray tracing;
- a five-company watchlist, Map concurrency limit, claim lease, document-size
  cap, and Lambda timeouts to bound work and SEC request pressure;
- an on-demand, encrypted DynamoDB filing registry with point-in-time recovery;
- private, encrypted and versioned S3 buckets for raw source documents and
  normalized corpus artifacts.

No queue is needed at the current bounded Map scale. DynamoDB condition
expressions and deterministic create-only S3 writes provide the idempotency
boundary. Blocks are published before the manifest commit marker. Terraform
state is local for now; a remote backend should be bootstrapped before multiple
people or automated deployment share this stack.

## Deploy

Prerequisites are Python 3.13, uv, Terraform 1.14+, AWS credentials for the
target account, and
permission to manage Lambda, IAM, Step Functions, EventBridge Scheduler,
CloudWatch Logs, X-Ray configuration, DynamoDB, and S3.

```bash
uv run python scripts/build_lambda_packages.py
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars
# Replace the example AWS account ID and SEC contact address before continuing.
aws sts get-caller-identity
terraform init
terraform plan -out=ingestion.tfplan
terraform apply ingestion.tfplan
```

The AWS provider checks `allowed_account_ids` before planning or applying, so
credentials for an unexpected account fail closed instead of creating a second
copy of the stack there. The reported account must also be the account already
represented by the current Terraform state. If it differs, select the intended
AWS credentials instead of bypassing the allowlist.

The schedule is disabled by default. This prevents an apply from immediately
making external requests and gives you a chance to verify a manual execution.
Reserved Lambda concurrency is also unset by default because small or new AWS
accounts may not have enough regional quota to reserve capacity while retaining
the service-required unreserved pool. If the account has sufficient headroom,
set `discovery_lambda_reserved_concurrency = 1` to impose a hard function-level
cap.
Acquisition Map concurrency defaults to two independently of that optional
discovery reservation.

## Verify manually

Use exact filing dates for a replayable smoke test:

```bash
STATE_MACHINE_ARN="$(terraform output -raw state_machine_arn)"
aws stepfunctions start-execution \
  --state-machine-arn "$STATE_MACHINE_ARN" \
  --input '{"provider":"sec","issuer_ids":["0000320193"],"forms":["10-K","10-Q"],"filed_from":"2025-01-01","filed_to":"2025-12-31"}'
```

Inspect the execution in Step Functions and confirm its output contains
`issuers_scanned`, `filings_found`, and one `filing_results` entry per filing.
A first successful result should contain acquisition `RAW_STORED` and
normalization `NORMALIZED`; replaying the same range should report both stages
as `ALREADY_COMPLETED`. Confirm the referenced raw object, manifest, compressed
blocks, and DynamoDB registry metadata. Lambda logs are under
`/aws/lambda/filing-corpus-pipeline-dev-discovery` and
`/aws/lambda/filing-corpus-pipeline-dev-acquisition` plus
`/aws/lambda/filing-corpus-pipeline-dev-normalization`; workflow logs are under
`/aws/vendedlogs/states/filing-corpus-pipeline-dev-discovery`.

Once the smoke test succeeds, set `schedule_enabled = true`, review another
plan, and apply it. EventBridge replaces `<aws.scheduler.scheduled-time>` for
each invocation; the Lambda derives and logs the inclusive discovery window.

The registry table uses `filing_key` as its only key and has no speculative
secondary indexes or TTL. Registry records are provenance and recovery state,
so they are retained. Enable `registry_deletion_protection_enabled` for a
long-lived environment after verifying the backup and teardown procedures.

Both S3 buckets block every form of public access, disable ACL-based ownership,
deny non-TLS requests, and apply SSE-S3 encryption. Versioning protects against
accidental overwrites. Current objects do not expire; noncurrent versions
expire after 30 days, incomplete multipart uploads after seven days, and
orphaned delete markers are removed. Generated names contain the AWS account ID
and a stable environment/Region hash.

The acquisition service uses deterministic keys shaped like
`raw/{provider}/{issuer_id}/{filing_id}/{document_name}` and stores the resulting
bucket/key/version, digest, and content metadata in the registry. Replays verify
the existing object's digest and length rather than overwriting it.

## Reconfigure or remove

Change the watchlist, forms, lookback, frequency, or log retention through
variables, then plan before applying. To stop recurring work without deleting
resources, set `schedule_enabled = false` and apply. To remove the whole slice:

```bash
terraform destroy
```

Deletion protection must be disabled and applied before Terraform can destroy
the registry table. Nonempty raw and normalized buckets also block destruction
by default. Set the corresponding `*_bucket_force_destroy` variable only when
every object version in an explicitly disposable environment may be deleted.
