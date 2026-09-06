"""Tests for reproducible normalized corpus artifacts."""

import gzip
import json
from datetime import date
from hashlib import sha256

import pytest

from filing_corpus_pipeline.domain import FilingReference
from filing_corpus_pipeline.normalization import (
    BLOCKS_FILENAME,
    NormalizedDocument,
    RawFilingDocument,
    SecHtmlNormalizer,
    SecHtmlParserConfig,
    normalized_document_prefix,
    render_artifacts,
)


def _document() -> NormalizedDocument:
    filing = FilingReference(
        company_id="example-issuer",
        provider="provider/one",
        provider_filing_id="filing #1",
        provider_issuer_id="issuer/1",
        issuer_name="Example Issuer",
        filing_type="quarterly-report",
        filed_on=date(2025, 4, 30),
        report_date=None,
        accepted_at=None,
        primary_document="report.htm",
        filing_detail_url="https://example.test/index",
        primary_document_url="https://example.test/report",
    )
    body = b"""
        <title>Example filing</title><h2>PART I</h2>
        <h3>ITEM 1. FINANCIAL STATEMENTS</h3>
        <table><tr><th>Metric</th><th>Value</th></tr><tr><td>Revenue</td><td>10</td></tr></table>
        <h3>ITEM 2. MANAGEMENT'S DISCUSSION AND ANALYSIS</h3><p>Demand improved.</p>
    """
    source_filing = filing.model_copy(update={"provider": "sec", "filing_type": "10-Q"})
    normalized = SecHtmlNormalizer(
        SecHtmlParserConfig(short_document_chars=1)
    ).normalize(
        RawFilingDocument(
            filing_key="provider/one#filing #1",
            filing=source_filing,
            body=body,
            content_type="text/html",
        )
    )
    return normalized.model_copy(update={"filing": filing})


def test_artifacts_are_reproducible_and_self_verifying() -> None:
    document = _document()
    first = render_artifacts(document)
    second = render_artifacts(document)
    manifest = json.loads(first.manifest)
    records = [
        json.loads(line)
        for line in gzip.decompress(first.blocks_jsonl_gzip).splitlines()
    ]

    assert first == second
    assert first.manifest.endswith(b"\n")
    assert gzip.decompress(first.blocks_jsonl_gzip).endswith(b"\n")
    assert first.manifest_sha256 == sha256(first.manifest).hexdigest()
    assert first.blocks_sha256 == sha256(first.blocks_jsonl_gzip).hexdigest()
    assert manifest["schema_version"] == "2"
    assert manifest["parser_version"] == "sec-html-v3"
    assert manifest["quality"] == {"status": "PASS", "warnings": []}
    assert manifest["statistics"]["block_count"] == len(records)
    assert manifest["statistics"]["table_count"] == 1
    assert manifest["artifacts"]["blocks"] == {
        "content_encoding": "gzip",
        "content_type": "application/x-ndjson",
        "filename": BLOCKS_FILENAME,
        "record_count": len(records),
        "sha256": first.blocks_sha256,
    }
    assert records[0]["ordinal"] == 0
    assert "processed_at" not in manifest


def test_normalized_prefix_escapes_identity_segments() -> None:
    document = _document()

    assert normalized_document_prefix(document) == (
        "normalized/provider%2Fone/issuer%2F1/filing%20%231/"
        "sec-html-v3/" + document.source_sha256
    )


def test_normalized_prefix_rejects_keys_over_s3_limit() -> None:
    document = _document()
    oversized_filing = document.filing.model_copy(
        update={"provider_filing_id": "x" * 1_000}
    )

    with pytest.raises(ValueError, match="key size"):
        normalized_document_prefix(
            document.model_copy(update={"filing": oversized_filing})
        )
