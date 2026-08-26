"""Tests for normalization orchestration and failure recovery policy."""

from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from typing import cast

import pytest

from filing_corpus_pipeline.domain import FilingForm, FilingReference
from filing_corpus_pipeline.normalization import (
    NormalizationOutcome,
    NormalizationRequest,
    PermanentNormalizationError,
    RetryableNormalizationError,
    SecHtmlNormalizer,
)
from filing_corpus_pipeline.normalization.service import NormalizationService
from filing_corpus_pipeline.registry import (
    ClaimOutcome,
    MarkNormalizationFailedRequest,
    MarkNormalizedRequest,
    NormalizationClaimRequest,
    NormalizationClaimResult,
    NormalizedCorpusMetadata,
    RawDocumentMetadata,
    RegistryStatus,
)
from filing_corpus_pipeline.registry.service import FilingRegistryService
from filing_corpus_pipeline.storage import (
    NormalizedCorpusWrite,
    RawObjectStorageError,
    S3NormalizedCorpusClient,
    S3RawDocumentClient,
    StoredNormalizedCorpus,
)

BODY = b"""<html><head><title>Example 10-Q</title></head><body>
<h1>PART I</h1><h2>Item 1. Financial Statements</h2>
<p>Revenue increased while operating costs remained controlled.</p>
</body></html>"""
DIGEST = sha256(BODY).hexdigest()
NOW = datetime(2025, 8, 1, 18, 0, tzinfo=UTC)


def filing() -> FilingReference:
    return FilingReference(
        provider="sec",
        provider_filing_id="0000320193-25-000079",
        provider_issuer_id="0000320193",
        issuer_name="Apple Inc.",
        form=FilingForm.TEN_Q,
        filed_on=date(2025, 8, 1),
        report_date=date(2025, 6, 28),
        accepted_at=NOW,
        primary_document="aapl.htm",
        filing_detail_url="https://example.test/index.html",
        primary_document_url="https://example.test/aapl.htm",
    )


def raw_document() -> RawDocumentMetadata:
    return RawDocumentMetadata(
        bucket="raw-bucket",
        key="raw/sec/apple/filing/aapl.htm",
        sha256=DIGEST,
        content_length=len(BODY),
        content_type="text/html",
        version_id="version-1",
    )


def corpus_metadata() -> NormalizedCorpusMetadata:
    return NormalizedCorpusMetadata(
        bucket="normalized-bucket",
        prefix="normalized/prefix",
        manifest_key="normalized/prefix/manifest.json",
        manifest_sha256="a" * 64,
        blocks_key="normalized/prefix/blocks.jsonl.gz",
        blocks_sha256="b" * 64,
        parser_version="sec-html-v2",
        schema_version="1",
        block_count=3,
        section_count=2,
        warning_count=1,
        quality_status="WARN",
    )


class StubRegistry:
    def __init__(self, claim: NormalizationClaimResult) -> None:
        self.claim_result = claim
        self.claims: list[NormalizationClaimRequest] = []
        self.normalized: list[MarkNormalizedRequest] = []
        self.failed: list[MarkNormalizationFailedRequest] = []

    def claim_normalization(
        self, request: NormalizationClaimRequest
    ) -> NormalizationClaimResult:
        self.claims.append(request)
        return self.claim_result

    def mark_normalized(self, request: MarkNormalizedRequest) -> None:
        self.normalized.append(request)

    def mark_normalization_failed(
        self, request: MarkNormalizationFailedRequest
    ) -> None:
        self.failed.append(request)


class StubRawStorage:
    def __init__(self, result: bytes | Exception = BODY) -> None:
        self.result = result
        self.loads: list[tuple[RawDocumentMetadata, int]] = []

    def load(self, document: RawDocumentMetadata, *, max_bytes: int) -> bytes:
        self.loads.append((document, max_bytes))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class StubCorpusStorage:
    def __init__(self) -> None:
        self.writes: list[NormalizedCorpusWrite] = []

    def store(self, request: NormalizedCorpusWrite) -> StoredNormalizedCorpus:
        self.writes.append(request)
        return StoredNormalizedCorpus(
            bucket="normalized-bucket",
            prefix=request.prefix,
            manifest_key=f"{request.prefix}/manifest.json",
            blocks_key=f"{request.prefix}/blocks.jsonl.gz",
            reused_manifest=False,
            reused_blocks=False,
        )


def acquired_claim() -> NormalizationClaimResult:
    return NormalizationClaimResult(
        filing_key="sec#0000320193-25-000079",
        outcome=ClaimOutcome.CLAIMED,
        status=RegistryStatus.NORMALIZING,
        attempt_count=1,
        raw_document=raw_document(),
        owner_id="execution-1",
    )


def request() -> NormalizationRequest:
    return NormalizationRequest(
        filing=filing(),
        owner_id="execution-1",
        requested_at=NOW,
        lease_duration=timedelta(minutes=10),
    )


def service(
    registry: StubRegistry,
    raw: StubRawStorage,
    corpus: StubCorpusStorage,
) -> NormalizationService:
    return NormalizationService(
        registry=cast(FilingRegistryService, registry),
        raw_storage=cast(S3RawDocumentClient, raw),
        corpus_storage=cast(S3NormalizedCorpusClient, corpus),
        normalizer=SecHtmlNormalizer(),
        max_document_bytes=1024,
        clock=lambda: NOW + timedelta(minutes=1),
    )


def test_normalize_publishes_artifacts_then_registry_metadata() -> None:
    registry = StubRegistry(acquired_claim())
    raw = StubRawStorage()
    corpus = StubCorpusStorage()

    result = service(registry, raw, corpus).normalize(request())

    assert result.outcome is NormalizationOutcome.NORMALIZED
    assert result.corpus is not None
    assert result.corpus.manifest_key.endswith("manifest.json")
    assert result.corpus.block_count >= 1
    assert raw.loads == [(raw_document(), 1024)]
    assert corpus.writes[0].source_sha256 == DIGEST
    assert registry.normalized[0].corpus == result.corpus
    assert registry.claims[0].parser_version == "sec-html-v2"


def test_normalize_returns_an_existing_corpus_without_reading_s3() -> None:
    existing = corpus_metadata()
    registry = StubRegistry(
        NormalizationClaimResult(
            filing_key="sec#filing",
            outcome=ClaimOutcome.ALREADY_COMPLETED,
            status=RegistryStatus.NORMALIZED,
            attempt_count=2,
            raw_document=raw_document(),
            corpus=existing,
        )
    )
    raw = StubRawStorage()
    corpus = StubCorpusStorage()

    result = service(registry, raw, corpus).normalize(request())

    assert result.outcome is NormalizationOutcome.ALREADY_COMPLETED
    assert result.corpus == existing
    assert raw.loads == []
    assert corpus.writes == []


def test_normalize_records_and_classifies_a_permanent_parse_failure() -> None:
    registry = StubRegistry(acquired_claim())
    raw = StubRawStorage(b"")

    with pytest.raises(PermanentNormalizationError) as raised:
        service(registry, raw, StubCorpusStorage()).normalize(request())

    assert raised.value.code == "EMPTY_SOURCE_DOCUMENT"
    assert registry.failed[0].failure.retryable is False
    assert registry.failed[0].parser_version == "sec-html-v2"


def test_normalize_records_and_classifies_a_retryable_raw_read_failure() -> None:
    registry = StubRegistry(acquired_claim())
    failure = RawObjectStorageError(
        "S3 unavailable", code="S3_SLOWDOWN", retryable=True
    )

    with pytest.raises(RetryableNormalizationError) as raised:
        service(registry, StubRawStorage(failure), StubCorpusStorage()).normalize(
            request()
        )

    assert raised.value.retryable is True
    assert registry.failed[0].failure.code == "S3_SLOWDOWN"
