# Discovery target contract

Discovery targets are versioned domain configuration, independent of the
EventBridge schedule. The source of truth for each environment is
`config/discovery-targets/<environment>.json`; Terraform uploads it to a
dedicated private S3 bucket and embeds the resulting exact object identity in
the Step Functions definition.

## Identity and scope

A company has an internal `company_id` that remains stable across regulators.
Each `registrations` entry contains the regulator-issued identity and the filing
and document selections for that specific company/regulator pair:

```json
{
  "company_id": "apple-inc",
  "display_name": "Apple Inc.",
  "registrations": [
    {
      "regulator": "sec",
      "issuer_id": "0000320193",
      "filings": [
        {
          "filing_type": "10-K",
          "document_policy": "primary"
        },
        {
          "filing_type": "10-Q",
          "document_policy": "primary"
        }
      ]
    }
  ]
}
```

Regulator identifiers remain strings so significant leading zeroes are never
lost. `(regulator, issuer_id)` is the globally unique registration identity and
may belong to only one configured company. Filing types are source-qualified,
unique and nonempty within a registration. The target-set and company IDs use
canonical lowercase letters, digits, and hyphens.

`filing_type` deliberately remains a string because filing taxonomies belong to
their regulator; there is no global SEC-shaped form enum. `document_policy` is
a closed pipeline capability whose implementation is stage-specific. Schema
version 2 recognizes `primary` and `earnings-release`; accepting a value in the
target contract does not imply that every pipeline stage supports it.

The generic discovery service groups registrations by regulator and routes each
group to a registered source. The deployed runtime currently registers only the
SEC source. SEC discovery supports `10-K`/`primary`, `10-Q`/`primary`, and
`8-K`/`earnings-release`. It validates every configured discovery route before
making any source request. Unknown regulators or unsupported
source/type/policy combinations fail explicitly rather than producing a partial
corpus.

The repository retains a narrow compatibility reader for pinned schema-v1
objects: their `filing_types` string lists are upgraded in memory to `primary`
selections while provenance continues to report schema version 1. New manifests
must use schema version 2; this compatibility path exists only for deterministic
replay of already-versioned configuration.

An earnings release is not represented as an alias for every `8-K`. The
selection uses `filing_type: "8-K"` with
`document_policy: "earnings-release"`. SEC discovery emits only exact `8-K`
records whose submissions metadata includes Item 2.02, excluding unrelated
current reports and amendments unless they are explicitly supported later. It
retains the accession, filing-detail URL, and primary-document metadata needed
by the next stage. A source-specific acquisition resolver will later inspect the
filing document list and select the relevant exhibit.

The source-controlled development manifest intentionally contains only the two
end-to-end routes, `10-K`/`primary` and `10-Q`/`primary`. Do not activate the
8-K selection there until acquisition and normalization support it; otherwise
the state machine would correctly discover the filing and then record a
permanent acquisition failure for the unsupported policy.

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
