# Filing Corpus Pipeline

Service and infrastructure for filing ingestion and processing.

## Requirements

- Python 3.13 or newer
- [uv](https://docs.astral.sh/uv/)
- Terraform 1.14 or newer (for infrastructure work)

## Set up development

```bash
uv sync
uv run pre-commit install
uv run pre-commit run --all-files
```

Commit the generated `uv.lock` file so local development and CI use the same
resolved dependencies.


## Run discovery locally

```bash
export SEC_USER_AGENT="filing-corpus-pipeline your-email@example.com"

uv run filing-corpus-pipeline discover \
  --cik 0000320193 \
  --filed-from 2025-01-01 \
  --form 10-K \
  --form 10-Q
```

The command queries SEC submission metadata and prints the JSON-compatible
payload that the parent Step Functions workflow receives. It does not download
filing documents or write pipeline state.

SEC automated access requires a declared user agent. Use a real monitored
contact address and do not commit it to the repository.

## Initial code boundaries

- `domain`: provider-neutral filing records passed between pipeline stages.
- `discovery`: the discovery request, result, provider port, and use case.
- `adapters/sec`: SEC-specific HTTP response parsing and record mapping.
- `entrypoints`: thin runtime composition for the local CLI and Lambda handler.

Later acquisition and processing stages should consume `FilingReference`
records without depending on SEC response formats.

## Lambda discovery contract

Configure the Lambda handler as:

```text
filing_corpus_pipeline.entrypoints.lambda_handler.handler
```

The parent workflow invokes it with an explicit, replayable date range:

```json
{
  "provider": "sec",
  "issuer_ids": ["0000320193", "0000789019"],
  "forms": ["10-K", "10-Q"],
  "filed_from": "2025-01-01",
  "filed_to": "2025-12-31"
}
```

`forms` may be omitted to select both supported forms. The Lambda environment
must contain `SEC_USER_AGENT` with a declared application name and monitored
contact address. Invalid input raises an exception so Step Functions records
the task as failed rather than receiving a partial result.

For recurring runs, EventBridge Scheduler supplies its execution time instead
of fixed dates:

```json
{
  "provider": "sec",
  "issuer_ids": ["0000320193", "0000789019"],
  "forms": ["10-K", "10-Q"],
  "scheduled_at": "<aws.scheduler.scheduled-time>",
  "lookback_days": 7
}
```

The handler converts `scheduled_at` to UTC and subtracts `lookback_days` to
produce the inclusive filing-date bounds. Exact and rolling date fields are
mutually exclusive, making every scheduled execution deterministic and
replayable.

## AWS discovery slice

Terraform under `infra/terraform` deploys the complete first vertical slice:

```text
EventBridge Scheduler -> Standard Step Functions -> discovery Lambda -> SEC
```

Step Functions owns the execution history and transient Lambda retry policy.
The Lambda is a small, dependency-free ZIP deployment with a bounded watchlist
and timeout, JSON logs, retained CloudWatch log groups, and X-Ray tracing.
EventBridge sends the scheduled timestamp plus the configured watchlist and
lookback window. Reserved concurrency is available as an opt-in control for AWS
accounts with sufficient regional quota.

The schedule is disabled by default. This makes deployment side-effect-safe:
first run an exact-date execution manually, inspect its workflow output and
logs, and only then enable recurring discovery. DynamoDB, object storage,
queues, filing downloads, and document processing intentionally remain outside
this slice.

To prepare a deployment:

```bash
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars
# Replace the AWS account ID and SEC contact address, then authenticate to AWS.
terraform init
terraform plan
```

See [`infra/terraform/README.md`](infra/terraform/README.md) for the deployment,
manual smoke-test, enablement, and teardown workflow.

## Quality checks

```bash
uv run black .
uv run ruff check .
uv run mypy
uv run pytest
terraform fmt -check -recursive infra/terraform
terraform -chdir=infra/terraform init -backend=false
terraform -chdir=infra/terraform validate
terraform -chdir=infra/terraform test
```

The same commands are available through `make format`, `make lint`,
`make typecheck`, `make test`, `make infra-validate`, `make infra-test`, and
`make check`.


GitHub Actions runs the Python quality suite and credential-free Terraform plan
tests for pushes to `main` and pull requests.


## Updating from the template

From a clean Git working tree:

```bash
uvx copier update
```

Review and test the resulting diff before committing it.
