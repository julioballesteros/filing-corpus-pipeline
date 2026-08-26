# Raw filing acquisition contract

Acquisition turns one provider-neutral `FilingReference` into a durable,
integrity-checked source object. Terraform deploys it as a dedicated Lambda
invoked once per filing by a bounded Step Functions `Map`.

## One invocation

```text
FilingReference
     │
     ▼
conditional DynamoDB claim
     │ acquired
     ▼
provider document source ── bounded HTTP GET
     │ bytes + response metadata
     ▼
SHA-256 + deterministic S3 key
     │
     ▼
create-only S3 PutObject
     │
     ▼
owned registry transition to RAW_STORED
```

The service result contains only the registry key, disposition, attempt count,
and S3 metadata. Filing bytes never cross a Step Functions state boundary.

## Provider boundary

`acquisition.FilingDocumentSource` is the small provider contract. This
abstraction exists because additional filing providers are an explicit roadmap
item. The first implementation,
`adapters.sec.documents.SecFilingDocumentSource`, derives the canonical SEC
archive URL from the CIK, accession number, and primary document name. It
rejects inconsistent workflow input before HTTP, supplies the required declared
user agent, imposes a timeout, and caps the response at 25 MiB by default.

Adding a provider means implementing this retrieval contract beside `sec` and
registering it during runtime composition. It does not require changing S3 or
registry policy.

## Object identity and idempotency

Raw documents use this deterministic key:

```text
raw/{provider}/{provider_issuer_id}/{provider_filing_id}/{primary_document}
```

Each identity segment is percent-escaped independently. The S3 client sends a
SHA-256 checksum and uses `If-None-Match: *`, so retries cannot silently create
another current object. If the key already exists, acquisition reads its object
metadata and reuses it only when both SHA-256 and byte length match. Different
bytes at the same deterministic key are a permanent `RAW_OBJECT_COLLISION`, not
an overwrite.

The registry retains bucket, key, SHA-256, length, content type, S3 version ID,
and ETag. S3 object metadata also retains the filing key, source URL, and
available source ETag/Last-Modified headers.

## Failure and recovery behavior

Expected provider and S3 failures are classified as retryable or permanent,
written to the registry, and raised as distinct acquisition error types. The
workflow retries only transient failures. Recording the failure releases the
claim.

- HTTP 429, 5xx, timeouts, network failures, S3 throttling, and S3 5xx errors
  are retryable.
- Invalid source identity, oversized or empty documents, other HTTP 4xx
  responses, and object collisions are permanent.
- An unexpected programming error leaves the time-bounded claim in `FETCHING`;
  a same-owner retry or expired-lease recovery can resume it without inventing
  a misleading data failure.
- If S3 succeeds but registry finalization fails, the service does not overwrite
  the claim with `FAILED`. A retry performs the create-only write, verifies and
  reuses the identical object, then attempts finalization again.

This boundary makes replay safe without introducing a queue, a second database,
or a generic storage abstraction for the first release.

## Workflow isolation

The parent workflow supplies its execution ARN as `claim_owner` and the Map
Task entry timestamp as the lease start. Map concurrency defaults to two and is
configurable up to five, keeping SEC request pressure and Lambda cost bounded.
`RAW_STORED` and `ALREADY_COMPLETED` route to the normalization Task in the same
Map iteration. Other dispositions stop without attempting to read an
incomplete raw object. After retries, an acquisition failure becomes a small
`FAILED` stage result containing its provider identity and error type; it does
not prevent unrelated filings from progressing. Full diagnostics remain in the
registry and logs.
