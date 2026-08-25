# Local SEC HTML normalization

## Scope

This round implements the deterministic transformation between an acquired raw
SEC primary document and storage-ready normalized corpus artifacts:

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
        manifest.json + blocks.jsonl.gz (in-memory bytes)
```

The code performs no network, S3, DynamoDB, or workflow calls. It therefore
remains fast to exercise locally and deterministic to replay. A following slice
will load raw bytes from S3, claim normalization work in the registry, persist
both artifacts, and expose the operation through its own Lambda handler.

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

Section detection ignores fragment-only table-of-contents links and bounds
heading length. Repeated Item headings receive distinct stable section IDs and
a data-quality warning instead of overwriting content.

This is corpus normalization, not semantic accounting extraction. Tables retain
their row/cell structure and visible inline-XBRL values, but XBRL concepts,
contexts, units, periods, dimensions, and numeric typing are intentionally not
modelled here. A later financial-facts stage can consume SEC's structured XBRL
data or acquired filing XBRL resources independently while retaining this
document corpus for narrative and RAG-oriented consumers.

## Output contract

The schema version is `1`; the parser version is `sec-html-v1`. They change for
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

These failures are non-retryable at the local parser layer: repeating the same
bytes and parser cannot change the result. The future orchestration layer may
retry S3 or registry operations separately.

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

## Deliberate next-slice boundary

The local implementation does not yet include:

- a normalization registry state, claim/lease, or safe completion transition;
- an S3 reader for the immutable raw object;
- a separate normalized-artifact bucket or create-only writes;
- a normalization Lambda package (including an architecture-compatible `lxml`
  build), handler, metrics, or tracing;
- Step Functions routing from successful acquisition to normalization; or
- semantic XBRL facts, narrative expectation extraction, chunking, embeddings,
  a RAG index, or a read API.

Keeping these outside this round lets the parser contract and corpus shape be
verified before infrastructure makes them expensive to change.
