# Discovery target contract

Discovery targets are versioned domain configuration, independent of the
EventBridge schedule. The source of truth for each environment is
`config/discovery-targets/<environment>.json`; Terraform uploads it to a
dedicated private S3 bucket and embeds the resulting exact object identity in
the Step Functions definition.

## Identity and scope

A company has an internal `company_id` that remains stable across regulators.
Each `registrations` entry contains the regulator-issued identity and the filing
types selected for that specific company/regulator pair:

```json
{
  "company_id": "apple-inc",
  "display_name": "Apple Inc.",
  "registrations": [
    {
      "regulator": "sec",
      "issuer_id": "0000320193",
      "filing_types": ["10-K", "10-Q"]
    }
  ]
}
```

Regulator identifiers remain strings so significant leading zeroes are never
lost. `(regulator, issuer_id)` is the globally unique registration identity and
may belong to only one configured company. Filing types are unique and nonempty
within a registration. The target-set and company IDs use canonical lowercase
letters, digits, and hyphens.

The schema deliberately permits regulator-specific filing-type strings, while
the current Lambda accepts only SEC registrations and the existing `10-K` and
`10-Q` types. Unsupported values fail the execution rather than disappearing
from the corpus silently. A future regulator source registry can route these
same registrations without changing the manifest shape.

## Version and integrity

Three values serve different purposes:

- `schema_version` controls compatibility with the target parser;
- `revision` is incremented whenever the target set changes semantically;
- the S3 `VersionId` and SHA-256 identify the exact deployed bytes.

S3 bucket versioning retains earlier configurations. Step Functions captures
the object version and digest in its definition, so executions already started
against an older definition continue to identify the configuration they used.
The Lambda reads that exact version, limits it to 256 KiB, checks its byte
length and SHA-256, then validates the strict Pydantic schema. Discovery output
retains all target provenance.

The concrete repository lives inside the `discovery` feature because target
schema and integrity failures are discovery policy, and S3 is the only deployed
backing store. It depends directly on the bounded reader in `storage.s3`; there
is no speculative repository interface or second interchangeable
implementation.

## Scheduling boundary

The recurring scheduler input contains only the scheduled timestamp and the
global rolling-window policy:

```json
{
  "scheduled_at": "<aws.scheduler.scheduled-time>",
  "lookback_days": 7
}
```

`lookback_days` applies to every registration in the execution. Manual
executions instead provide `filed_from` and `filed_to`. Step Functions wraps
either input under `window` and attaches `target_config`; callers cannot select
an arbitrary S3 object through execution input.

## Changing targets

1. Edit `config/discovery-targets/<environment>.json`.
2. Increment `revision`.
3. Run the Python and Terraform test suites; tests validate the real manifest.
4. Review `terraform plan`. It should create a new object version and update the
   state-machine definition to its version ID and digest.
5. Apply, then run an exact-date smoke test before enabling or retaining the
   recurring schedule.

Do not edit a deployed S3 object manually. Terraform and the source-controlled
manifest are the configuration authority.
