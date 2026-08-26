# SEC HTML normalization

## Scope

The normalization stage implements the deterministic transformation between an
acquired raw SEC primary document and a committed, query-ready corpus version:

```text
raw HTML bytes + FilingReference + expected SHA-256
                         |
                         v
                SecHtmlNormalizer
                         |
                         v
       NormalizedDocument (sections + ordered blocks + warnings)
                         |
                         v
              manifest.json + blocks.jsonl.gz
                         |
                         v
       create-only normalized S3 objects + registry metadata
```

The parser and renderer remain pure local code and are fast to replay. The
`NormalizationService` wraps that core with a separate DynamoDB claim, bounded
and integrity-verified S3 raw read, immutable artifact publication, and an
owned registry completion. Step Functions invokes the service through a
dedicated Lambda after successful or previously completed acquisition.

## Input contract

`RawFilingDocument` contains:

- the provider-qualified registry key and provider-neutral `FilingReference`;
- the exact acquired document bytes and source content type;
- the acquisition SHA-256 digest when available.

The digest is verified before parsing. This makes corruption or a mismatched
raw object a permanent, visible failure rather than allowing output with false
provenance.

## Parsing behavior

`SecHtmlNormalizer` uses `lxml`'s recovery-capable HTML parser with network
access and unbounded-tree support disabled. It:

1. removes scripts, styles, comments, hidden elements, and inline-XBRL header,
   hidden, reference, and resource infrastructure;
2. preserves visible inline-XBRL facts as normal text or table-cell values;
3. normalizes Unicode and whitespace;
4. emits ordered headings, paragraphs, list items, preformatted text, and
   tables without duplicating container text;
5. tracks Parts and Items, using the form and Part to assign canonical 10-K or
   10-Q section names;
6. gives every block a stable ordinal, content digest, and ID; and
7. creates compact section index records for downstream consumers.

Section detection ignores fragment links, bounds heading length, and requires
structural evidence such as a native heading, emphasized text, uppercase text,
or an exact form/Part-specific Item title. One-row emphasized layout tables are
converted to headings; ordinary tables retain their row/cell structure. This
handles common SEC presentation markup without treating narrative paragraphs
that merely begin with an Item cross-reference as new sections. Repeated true
Item headings receive distinct stable section IDs and a data-quality warning
instead of overwriting content.

This is corpus normalization, not semantic accounting extraction. Tables retain
their row/cell structure and visible inline-XBRL values, but XBRL concepts,
contexts, units, periods, dimensions, and numeric typing are intentionally not
modelled here. A later financial-facts stage can consume SEC's structured XBRL
data or acquired filing XBRL resources independently while retaining this
document corpus for narrative and RAG-oriented consumers.

## Output contract

The schema version is `1`; the parser version is `sec-html-v2`. They change for
different reasons:

- increment the schema version when a downstream-facing field or meaning
  changes;
- increment the parser version when extraction/classification behavior changes
  enough to require reprocessing.

`render_artifacts` creates:

- `blocks.jsonl.gz`: one compact JSON object per ordered block, with
  deterministic key ordering and a gzip timestamp of zero;
- `manifest.json`: filing/source provenance, versions, document title, quality
  status and warnings, statistics, section indexes, and the compressed block
  artifact's digest and record count.

No processing timestamp is embedded, so identical input, filing metadata,
schema, and parser code produce identical bytes. The intended object prefix is:

```text
normalized/{provider}/{issuer-id}/{filing-id}/{parser-version}/{source-sha256}
```

Every identity segment is URL-escaped. Including both parser version and source
digest makes reprocessing create-addressed and safe: a retry targets identical
keys, a parser upgrade writes a new version, and changed source bytes cannot be
mistaken for the previous output.

The S3 storage client publishes `blocks.jsonl.gz` first and `manifest.json`
last. Both writes use `If-None-Match: *`, an S3 checksum, encryption, and stored
SHA-256 metadata. A retry reuses an object only when its digest and byte length
match. Therefore the manifest is a small commit marker: consumers that discover
it never observe a corpus version whose referenced blocks were not published
first.

## Failure and quality policy

Hard failures reject output and carry bounded machine-readable codes:

| Code | Meaning |
| --- | --- |
| `EMPTY_SOURCE_DOCUMENT` | No acquired bytes were supplied. |
| `SOURCE_DIGEST_MISMATCH` | Bytes do not match acquisition provenance. |
| `DOCUMENT_TOO_LARGE` | Input exceeds the configured byte bound. |
| `INVALID_HTML` | The bounded recovery parser could not build a document. |
| `NO_CONTENT_BLOCKS` | No visible corpus content remained. |
| `PARSER_RESOURCE_LIMIT` | Block or table-cell output exceeded a bound. |

These failures are non-retryable at the parser layer: repeating the same bytes
and parser cannot change the result. The orchestration layer separately marks
transient S3 and registry failures as retryable so Step Functions consumes
retries only when another attempt can plausibly succeed.

Parseable content is retained with `WARN` quality status for short documents,
missing Item structure, missing high-value expected sections, or duplicate Item
headings. This avoids silently dropping unusual but potentially valuable
filings while making degraded records queryable and measurable.

Default per-document limits are 25 MiB of input, 50,000 blocks, and 250,000
table cells. They are explicit configuration rather than ambient process limits
and have focused tests.

## Local use

```python
from filing_corpus_pipeline.normalization import (
    RawFilingDocument,
    SecHtmlNormalizer,
    render_artifacts,
)

source = RawFilingDocument(
    filing_key=filing_key,
    filing=filing_reference,
    body=raw_html,
    content_type="text/html",
    expected_sha256=acquisition_sha256,
)
document = SecHtmlNormalizer().normalize(source)
artifacts = render_artifacts(document)
```

Representative, intentionally abbreviated inline-XBRL 10-K and 10-Q fixtures
exercise section mapping, visible facts, hidden infrastructure, tables,
malformed markup, quality findings, resource limits, and byte-for-byte artifact
reproducibility. They are synthetic so the repository does not redistribute a
company filing fixture or couple its tests to a mutable upstream document.

## Deployed boundary and deliberate exclusions

Terraform deploys the normalization Lambda, its private versioned corpus
bucket, least-privilege registry/raw/corpus permissions, retained JSON logs,
X-Ray tracing, and the acquisition-to-normalization workflow route. Its Lambda
ZIP is built from the locked Python 3.13 Linux arm64 Pydantic and `lxml` wheels
rather than assuming developer-workstation binaries will run in AWS.

Still outside this ingestion project are semantic XBRL facts, narrative
expectation extraction, consumer-specific chunking, embeddings, a vector/RAG
index, and a read API. Those are independent downstream stages or services and
do not belong in the one-week ingestion vertical slice.
