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
- `config/discovery-targets`: source-controlled company identities, regulator
  registrations, and per-registration filing types deployed with the stack.
- `acquisition`: raw-document retrieval orchestration and its provider contract.
- `normalization`: deterministic SEC HTML parsing, section classification, data
  quality findings, and versioned corpus artifact rendering.
- `adapters/sec`: SEC metadata/document retrieval, response parsing, and record
  mapping. Future document providers belong beside it.
- `registry`: filing claim models and the concrete registry service.
- `storage`: narrow DynamoDB and S3 clients plus SDK serialization details.
- `entrypoints`: separate thin handlers for discovery, acquisition, and
  normalization plus the local discovery CLI.

Acquisition and normalization consume `FilingReference` records without
depending on SEC metadata response formats.

Contracts that cross a Lambda, Step Functions, parser, or persistence boundary
are immutable Pydantic models with forbidden extra fields and JSON-native
serialization. Small implementation-only records and configuration objects
remain frozen dataclasses; they do not need runtime schema machinery.

## Lambda discovery contract

Configure the Lambda handler as:

```text
filing_corpus_pipeline.entrypoints.discovery_lambda.handler
```

The parent workflow invokes it with an explicit, replayable date range and the
exact version of the deployed target manifest:

```json
{
  "window": {
    "filed_from": "2025-01-01",
    "filed_to": "2025-12-31"
  },
  "target_config": {
    "bucket": "filing-corpus-pipeline-dev-config-...",
    "key": "discovery-targets/dev.json",
    "version_id": "...",
    "sha256": "..."
  }
}
```

The target manifest contains stable internal company IDs and one or more
regulator registrations per company. Filing types belong to a registration, so
two companies or two regulators need not share a form selection. Discovery is
still SEC-only in this release; a configured non-SEC regulator fails explicitly
until regulator source routing is introduced. The Lambda environment must
contain `SEC_USER_AGENT` with a declared application name and monitored contact
address.

For recurring runs, EventBridge Scheduler supplies only its execution time and
the global rolling-window policy:

```json
{
  "scheduled_at": "<aws.scheduler.scheduled-time>",
  "lookback_days": 7
}
```

Step Functions wraps that input as `window` and attaches the target object's S3
bucket, key, version ID, and expected SHA-256. The handler loads that exact
version, checks its size and digest, validates its strict schema, and converts
`scheduled_at` to an inclusive UTC filing-date window. The discovery output
retains the target-set ID, logical revision, S3 version, and digest for audit and
replay.

See [`docs/discovery-targets.md`](docs/discovery-targets.md) for company and
registration identity, target versioning, deployment, and change procedures.

## Lambda acquisition contract

Each Step Functions `Map` iteration invokes the dedicated acquisition handler:

```text
filing_corpus_pipeline.entrypoints.acquisition_lambda.handler
```

Its event contains one discovery record, the workflow execution ARN used as the
claim owner, and the Task entry time used to start the lease:

```json
{
  "filing": { "provider": "sec", "provider_filing_id": "..." },
  "owner_id": "arn:aws:states:...:execution:...",
  "requested_at": "2025-08-01T18:00:00Z"
}
```

Feature-specific composition lives in `discovery/composition.py` and
the corresponding `acquisition` and `normalization` packages; the handlers only
validate runtime input and configuration, call their service, log the bounded
result, and serialize it.

## Lambda normalization contract

After acquisition reports `RAW_STORED` or `ALREADY_COMPLETED`, the same Map
iteration invokes:

```text
filing_corpus_pipeline.entrypoints.normalization_lambda.handler
```

It receives the same provider-neutral filing record, workflow owner, and Task
timestamp. It does not receive document bytes or trust acquisition output for
storage identity: it atomically claims normalization in DynamoDB, loads the raw
bucket/key/version recorded by acquisition, verifies length and SHA-256, and
returns only committed corpus metadata.

## AWS ingestion slice

Terraform under `infra/terraform` deploys the complete first vertical slice:

```text
Source-controlled targets -> versioned S3 config ---------------------+
                                                                    |
EventBridge Scheduler -> Standard Step Functions -> discovery Lambda -> SEC
                                            |
                                            +-> bounded Map
                                                  |
                                                  +-> acquisition Lambda
                                                  |     |-> SEC document
                                                  |     |-> DynamoDB registry
                                                  |     +-> S3 raw object
                                                  |
                                                  +-> normalization Lambda
                                                        |-> S3 raw object
                                                        |-> DynamoDB registry
                                                        +-> S3 normalized corpus
```

Step Functions owns the execution history, per-filing concurrency, and retry
policy. All three deployment ZIPs are built reproducibly for Python 3.13 on
Lambda arm64 and contain the Pydantic runtime pinned by `uv.lock`;
normalization additionally contains the pinned Linux `lxml` wheel. Each ZIP is
limited to the application modules required by its handler. All three functions
have explicit handlers, resource bounds, JSON logs, retained CloudWatch log
groups, and X-Ray tracing.
EventBridge sends only the scheduled timestamp and global lookback window.
Terraform deploys the target set separately and pins its exact S3 object version
in the state-machine definition. Reserved concurrency is available as an opt-in
control for AWS accounts with sufficient regional quota.

The schedule is disabled by default. This makes deployment side-effect-safe:
first run an exact-date execution manually, inspect its workflow output and
logs, verify the registry plus raw and normalized objects, and only then enable
recurring ingestion.

## Document normalization

The first processing round converts one acquired SEC primary HTML document
into a provider-neutral `NormalizedDocument`. It removes non-content and hidden
inline-XBRL infrastructure, emits ordered text/table blocks, maps 10-K and 10-Q
Item headings to canonical section names, and reports non-fatal quality
findings. The result renders as a self-describing `manifest.json` plus
reproducible `blocks.jsonl.gz` bytes. In AWS, blocks are written create-only
before `manifest.json`; the manifest is the commit marker downstream consumers
can use to distinguish a complete corpus version. Parser upgrades safely
reclaim a filing and publish under a new versioned prefix.

Semantic XBRL fact extraction remains a separate future stage; the normalizer
preserves visible inline-XBRL values as document text and table cells. See
[`docs/normalization.md`](docs/normalization.md) for contracts, failure policy,
versioning, storage ordering, and local usage.

## Filing registry

The registry uses the provider-qualified filing ID as its DynamoDB partition
key. An acquisition worker claims work with one conditional update—never a
race-prone read followed by a write. Claims carry an owner and an expiry, so a
Step Functions retry can resume its own work and a later execution can recover
an abandoned lease.

The persisted lifecycle continues through `NORMALIZING`, `NORMALIZED`, and
`NORMALIZATION_FAILED`. Acquisition and normalization have separate leases,
attempt counts, and bounded failures. Retryable failures can be reclaimed;
permanent failures remain visible, while a new parser version may intentionally
reprocess an old result or permanent parser failure. Only the active owner may
complete a transition. See
[`docs/filing-registry.md`](docs/filing-registry.md) for the item schema,
transition rules, and recovery cases.

## Raw document storage

The private S3 bucket is the durable boundary for byte-for-byte source filings.
It has account-enforced ownership, complete public-access blocking, HTTPS-only
access, default encryption, versioning, and bounded cleanup of noncurrent data.
Current source documents have no expiration because they are corpus provenance.

The bucket name is stable for an account, environment, and Region without being
globally collision-prone. Terraform refuses to destroy a populated bucket by
default. The acquisition service writes deterministic object keys and returns
only S3 metadata—never the document body. It uses a create-only write and
verifies an existing object's SHA-256 and length before treating a retry as
successful. See [`docs/acquisition.md`](docs/acquisition.md) for provider
validation, object-key, failure, and recovery contracts.

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
uv run python scripts/build_lambda_packages.py
terraform -chdir=infra/terraform init -backend=false
terraform -chdir=infra/terraform validate
terraform -chdir=infra/terraform test
```

The same commands are available through `make format`, `make lint`,
`make typecheck`, `make test`, `make infra-validate`, `make infra-test`, and
`make check`. Infrastructure validation and tests build all three Lambda ZIPs
automatically from versions pinned in `uv.lock`.


GitHub Actions runs the Python quality suite and credential-free Terraform plan
tests for pushes to `main` and pull requests.


## Updating from the template

From a clean Git working tree:

```bash
uvx copier update
```

Review and test the resulting diff before committing it.
