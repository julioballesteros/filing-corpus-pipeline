# Filing registry contract

The filing registry is the idempotency and recovery boundary across discovery,
acquisition, and normalization. Discovery is intentionally overlapping, so
seeing an existing filing is normal rather than exceptional.

## Identity and item shape

`filing_key` is the only DynamoDB key. It combines escaped provider and filing
identities, for example `sec#0000320193-25-000079`. A unique partition key per
filing distributes writes and avoids a provider-wide hot partition.

The first successful claim stores:

| Group | Attributes |
| --- | --- |
| Identity | `filing_key`, `company_id`, `provider`, `provider_filing_id`, `provider_issuer_id` |
| Source metadata | issuer, filing type, document policy, filing/report dates, source URLs and primary document |
| Claim | `status`, `claim_owner`, `lease_expires_at_epoch`, `attempt_count` |
| Audit | `first_discovered_at`, `last_claimed_at`, `updated_at`, `schema_version` |
| Raw object | S3 bucket/key/version, ETag, SHA-256, length, content type, selected source-document identity and storage timestamp |
| Normalization claim | owner, lease expiry, parser version, attempt count and last claim time |
| Corpus | normalized bucket/prefix, manifest and block keys/digests, schema/parser versions, quality status and counts |
| Failures | separate bounded acquisition and normalization error fields, timestamps and retryability |

Nullable filing metadata uses DynamoDB `NULL` values rather than missing fields.
Schema version three adds acquisition-time source provenance:
`raw_source_document_name`, `raw_source_document_type`,
`raw_source_document_description`, `raw_source_url`, and
`raw_resolver_version`. Claim and failure attributes are removed when they no
longer describe the current state.

Normalization remains compatible with schema-version-two items already stored
before this addition. When the new raw-source fields are entirely absent, it
reconstructs the selected identity from `primary_document`, `filing_type`, and
`primary_document_url` and labels it `legacy-primary-v1`. A partial version-three
provenance record is rejected as corrupt rather than silently mixing schemas.

## State transitions

```text
missing ───────────────► FETCHING ───────────────► RAW_STORED
                           │                            │
                           └────────► FAILED            ▼
                                        │          NORMALIZING
                                        │              │
                                        │              ├────────► NORMALIZED
                                        │              └────────► NORMALIZATION_FAILED
                                        │                              │
                                        ├─ retryable ─► FETCHING       ├─ retryable ─► NORMALIZING
                                        └─ permanent: visible          └─ same parser: visible

FETCHING with expired lease ──────────► FETCHING (new owner)
FETCHING with the same owner ─────────► FETCHING (Step Functions retry)
NORMALIZING with expired lease ───────► NORMALIZING (new owner)
NORMALIZING with the same owner ──────► NORMALIZING (Step Functions retry)
NORMALIZED or normalization failure with a new parser version ─► NORMALIZING
```

Only `FETCHING` owned by the caller can transition to `RAW_STORED` or `FAILED`.
This prevents a slow or retried Lambda invocation from overwriting a newer
worker's result.

Normalization uses separate owner/lease/attempt/failure attributes, so it does
not erase acquisition provenance. Only `NORMALIZING` owned by the caller may
become `NORMALIZED` or `NORMALIZATION_FAILED`. The raw object metadata and the
identity of the document actually selected by acquisition remain available in
every post-acquisition state; Step Functions never needs to carry S3 metadata
between tasks.

## Atomic claim behavior

The registry service asks its DynamoDB storage client to perform a conditional
`UpdateItem`. The update succeeds only when the item is missing, has a retryable
failure, has an expired lease, or is already owned by the same workflow
execution. It also increments the attempt counter and refreshes source metadata
atomically.

When the condition fails, a strongly consistent projected read classifies the
current item as:

- `ALREADY_COMPLETED` for any post-acquisition state;
- `ALREADY_IN_PROGRESS` for another active owner; or
- `NOT_RETRYABLE` for a permanent failure.

The read does not provide the uniqueness guarantee—the preceding conditional
write does. A short retry handles the narrow race where an item changes between
the failed condition and classification.

Normalization performs an equivalent conditional claim from `RAW_STORED`, a
retryable `NORMALIZATION_FAILED`, an expired/same-owner `NORMALIZING`, or an old
parser version. A same-version `NORMALIZED` item returns its stored corpus
metadata as `ALREADY_COMPLETED`; a same-version permanent failure remains
`NOT_RETRYABLE`. This makes overlapping discovery cheap while turning parser
upgrades into explicit, safe reprocessing.

## Infrastructure choices

- On-demand billing matches a small, bursty portfolio workload.
- AWS-managed encryption is enabled without introducing a customer-managed KMS
  key and its policy surface.
- Point-in-time recovery is always enabled.
- Deletion protection is configurable and off in the disposable development
  stack.
- TTL is disabled because registry records provide durable provenance.
- No secondary index is created until an operational query requires one.

The concrete `registry` service owns state-transition and conflict policy. The
`storage.dynamodb` client owns DynamoDB expressions, serialization, consistent
reads, and SDK error translation. There is no abstract registry service because
there is only one implementation today.

The acquisition and normalization Lambdas each have only `GetItem` and
`UpdateItem` access to this table. Step Functions supplies its execution ID as
the owner, so retries within one execution recover the same claim identity
while overlapping executions are isolated by the lease.
