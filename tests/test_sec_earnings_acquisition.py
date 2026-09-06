"""Integration tests for SEC earnings resolution through durable acquisition."""

from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from typing import cast

import pytest

from filing_corpus_pipeline.acquisition import (
    AcquisitionOutcome,
    AcquisitionRequest,
    AcquisitionService,
    FilingDocumentSource,
    PermanentAcquisitionError,
)
from filing_corpus_pipeline.acquisition.sec import (
    SecDocumentConfig,
    SecFilingDocumentSource,
)
from filing_corpus_pipeline.domain import DocumentPolicy, FilingReference
from filing_corpus_pipeline.registry import (
    ClaimOutcome,
    ClaimRequest,
    ClaimResult,
    FilingRegistryService,
    MarkFailedRequest,
    MarkRawStoredRequest,
    RegistryStatus,
)
from filing_corpus_pipeline.sources.http import HttpBytesResponse
from filing_corpus_pipeline.sources.sec import SecEdgarClient, SecEdgarClientConfig
from filing_corpus_pipeline.storage.raw_documents import (
    RawObjectWrite,
    S3RawDocumentClient,
    StoredRawObject,
)


class ScriptedSecTransport:
    """Return bounded SEC archive responses in request order."""

    def __init__(self, *responses: bytes) -> None:
        self.responses = list(responses)
        self.calls: list[str] = []

    def get_bytes(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout_seconds: float,
        max_bytes: int,
    ) -> HttpBytesResponse:
        del headers, timeout_seconds, max_bytes
        self.calls.append(url)
        return HttpBytesResponse(
            body=self.responses.pop(0),
            content_type="text/html",
            etag='"source-etag"',
            last_modified="Wed, 03 Sep 2025 12:00:00 GMT",
        )

    def get_json(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> object:
        del url, headers, timeout_seconds
        raise AssertionError("earnings acquisition must use SEC archive requests")


class CapturingRegistry:
    """Implement the feature-level registry boundary for one claimed filing."""

    def __init__(self) -> None:
        self.claims: list[ClaimRequest] = []
        self.stored: list[MarkRawStoredRequest] = []
        self.failed: list[MarkFailedRequest] = []

    def claim(self, request: ClaimRequest) -> ClaimResult:
        self.claims.append(request)
        return ClaimResult(
            filing_key="sec#0000320193-25-000079",
            outcome=ClaimOutcome.CLAIMED,
            status=RegistryStatus.FETCHING,
            attempt_count=1,
            owner_id=request.owner_id,
        )

    def mark_raw_stored(self, request: MarkRawStoredRequest) -> None:
        self.stored.append(request)

    def mark_failed(self, request: MarkFailedRequest) -> None:
        self.failed.append(request)


class CapturingRawStorage:
    """Capture the exact object produced after SEC document selection."""

    def __init__(self) -> None:
        self.writes: list[RawObjectWrite] = []

    def store(self, request: RawObjectWrite) -> StoredRawObject:
        self.writes.append(request)
        return StoredRawObject(
            bucket="filing-corpus-raw",
            key=request.key,
            version_id="version-1",
            etag='"s3-etag"',
            reused=False,
        )


def filing() -> FilingReference:
    """Build the Item 2.02 discovery output consumed by acquisition."""
    archive = "https://www.sec.gov/Archives/edgar/data/320193/000032019325000079"
    return FilingReference(
        company_id="apple-inc",
        provider="sec",
        provider_filing_id="0000320193-25-000079",
        provider_issuer_id="0000320193",
        issuer_name="Apple Inc.",
        filing_type="8-K",
        document_policy=DocumentPolicy.EARNINGS_RELEASE,
        filed_on=date(2025, 9, 3),
        report_date=date(2025, 9, 3),
        accepted_at=datetime(2025, 9, 3, 12, tzinfo=UTC),
        primary_document="report.htm",
        filing_detail_url=f"{archive}/0000320193-25-000079-index.html",
        primary_document_url=f"{archive}/report.htm",
    )


def filing_detail(*rows: str) -> bytes:
    """Build the source table consumed by the real SEC inventory parser."""
    return (
        '<table summary="Document Format Files">'
        "<tr><th>Seq</th><th>Description</th><th>Document</th>"
        "<th>Type</th><th>Size</th></tr>"
        f"{''.join(rows)}"
        "</table>"
    ).encode()


def row(
    sequence: int,
    description: str,
    document_name: str,
    document_type: str,
) -> str:
    """Build one SEC source-document row."""
    return (
        "<tr>"
        f"<td>{sequence}</td><td>{description}</td>"
        f'<td><a href="{document_name}">{document_name}</a></td>'
        f"<td>{document_type}</td><td>100</td>"
        "</tr>"
    )


def service(
    transport: ScriptedSecTransport,
    registry: CapturingRegistry,
    storage: CapturingRawStorage,
) -> AcquisitionService:
    """Compose the real SEC source with the generic acquisition service."""
    source = SecFilingDocumentSource(
        client=SecEdgarClient(
            transport=transport,
            config=SecEdgarClientConfig(
                user_agent="filing-corpus test@example.com",
                request_interval_seconds=0,
            ),
        ),
        config=SecDocumentConfig(
            max_document_bytes=1024,
            max_filing_detail_bytes=4096,
        ),
    )
    return AcquisitionService(
        registry=cast(FilingRegistryService, registry),
        raw_storage=cast(S3RawDocumentClient, storage),
        sources=[cast(FilingDocumentSource, source)],
        clock=lambda: datetime(2025, 9, 3, 12, 1, tzinfo=UTC),
    )


def request() -> AcquisitionRequest:
    """Build the stable Map-item acquisition request."""
    return AcquisitionRequest(
        filing=filing(),
        owner_id="execution-1",
        requested_at=datetime(2025, 9, 3, 12, tzinfo=UTC),
        lease_duration=timedelta(minutes=5),
    )


def test_selected_earnings_exhibit_reaches_storage_and_registry() -> None:
    """Resolver provenance survives the complete acquisition transaction."""
    detail = filing_detail(
        row(1, "8-K", "report.htm", "8-K"),
        row(2, "Investor presentation", "slides.htm", "EX-99.1"),
        row(3, "Quarterly financial results", "earnings.htm", "EX-99.2"),
    )
    body = b"<html><body>Quarterly results</body></html>"
    transport = ScriptedSecTransport(detail, body)
    registry = CapturingRegistry()
    storage = CapturingRawStorage()

    result = service(transport, registry, storage).acquire(request())

    expected_url = (
        "https://www.sec.gov/Archives/edgar/data/320193/"
        "000032019325000079/earnings.htm"
    )
    assert result.outcome is AcquisitionOutcome.RAW_STORED
    assert result.document is not None
    assert result.document.source_document.document_name == "earnings.htm"
    assert result.document.source_document.provider_document_type == "EX-99.2"
    assert result.document.source_document.source_url == expected_url
    assert result.document.source_document.resolver_version == (
        "sec-earnings-release-v1"
    )
    assert result.document.sha256 == sha256(body).hexdigest()
    assert storage.writes[0].key.endswith("/earnings.htm")
    assert storage.writes[0].body == body
    assert storage.writes[0].source_document == result.document.source_document
    assert registry.stored[0].document == result.document
    assert registry.failed == []
    assert transport.calls == [filing().filing_detail_url, expected_url]


def test_ambiguous_exhibits_are_recorded_as_a_permanent_failure() -> None:
    """The service releases its claim without writing arbitrary exhibit bytes."""
    detail = filing_detail(
        row(2, "Quarterly results", "results.htm", "EX-99.2"),
        row(3, "Financial results", "financials.htm", "EX-99.3"),
    )
    transport = ScriptedSecTransport(detail)
    registry = CapturingRegistry()
    storage = CapturingRawStorage()

    with pytest.raises(PermanentAcquisitionError) as raised:
        service(transport, registry, storage).acquire(request())

    assert raised.value.code == "SEC_EARNINGS_RELEASE_AMBIGUOUS"
    assert storage.writes == []
    assert registry.stored == []
    assert registry.failed[0].failure.code == "SEC_EARNINGS_RELEASE_AMBIGUOUS"
    assert registry.failed[0].failure.retryable is False
    assert transport.calls == [filing().filing_detail_url]
