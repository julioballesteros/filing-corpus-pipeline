# Filing registry contract

The filing registry is the idempotency and recovery boundary between discovery
and acquisition. Discovery is intentionally overlapping, so seeing an existing
filing is normal rather than exceptional.

## Identity and item shape

`filing_key` is the only DynamoDB key. It combines escaped provider and filing
identities, for example `sec#0000320193-25-000079`. A unique partition key per
filing distributes writes and avoids a provider-wide hot partition.

The first successful claim stores:

| Group | Attributes |
| --- | --- |
| Identity | `filing_key`, `provider`, `provider_filing_id`, `provider_issuer_id` |
| Source metadata | issuer, form, filing/report dates, source URLs and primary document |
| Claim | `status`, `claim_owner`, `lease_expires_at_epoch`, `attempt_count` |
| Audit | `first_discovered_at`, `last_claimed_at`, `updated_at`, `schema_version` |
| Raw object | S3 bucket/key/version, ETag, SHA-256, length, content type and storage timestamp |
| Failure | bounded error code/message, failure timestamp and `retryable` classification |

Nullable filing metadata uses DynamoDB `NULL` values rather than missing fields,
making the version-one shape explicit. Claim and failure attributes are removed
when they no longer describe the current state.

## State transitions

```text
missing ───────────────► FETCHING ───────────────► RAW_STORED
                           │                            │
                           └────────► FAILED            └─► duplicate: skip
                                        │
                                        ├─ retryable ─► FETCHING
                                        └─ permanent ─► visible, do not retry

FETCHING with expired lease ──────────► FETCHING (new owner)
FETCHING with the same owner ─────────► FETCHING (Step Functions retry)
```

Only `FETCHING` owned by the caller can transition to `RAW_STORED` or `FAILED`.
This prevents a slow or retried Lambda invocation from overwriting a newer
worker's result.

## Atomic claim behavior

The registry service asks its DynamoDB storage client to perform a conditional
`UpdateItem`. The update succeeds only when the item is missing, has a retryable
failure, has an expired lease, or is already owned by the same workflow
execution. It also increments the attempt counter and refreshes source metadata
atomically.

When the condition fails, a strongly consistent projected read classifies the
current item as:

- `ALREADY_COMPLETED` for `RAW_STORED`;
- `ALREADY_IN_PROGRESS` for another active owner; or
- `NOT_RETRYABLE` for a permanent failure.

The read does not provide the uniqueness guarantee—the preceding conditional
write does. A short retry handles the narrow race where an item changes between
the failed condition and classification.

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

The table is deployed now but has no acquisition runtime permissions or
workflow coupling. The acquisition service implements these transitions
locally. Its next Lambda composition will receive exact item-level API
permissions on this table and supply the Step Functions execution ID as
`claim_owner`.
