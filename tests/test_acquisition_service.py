"""Tests for acquisition orchestration independent of AWS SDK syntax."""

from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from typing import cast

import pytest

from filing_corpus_pipeline.acquisition import (
    AcquisitionOutcome,
    AcquisitionRequest,
    AcquisitionService,
    DocumentRetrievalError,
    FilingDocumentSource,
    PermanentAcquisitionError,
    RetrievedDocument,
    RetryableAcquisitionError,
)
from filing_corpus_pipeline.domain import FilingForm, FilingReference
from filing_corpus_pipeline.registry import (
    ClaimOutcome,
    ClaimRequest,
    ClaimResult,
    FilingRegistryService,
    MarkFailedRequest,
    MarkRawStoredRequest,
    RegistryStatus,
)
from filing_corpus_pipeline.storage.raw_documents import (
    RawObjectStorageError,
    RawObjectWrite,
    S3RawDocumentClient,
    StoredRawObject,
)


class StubRegistryService:
    """Record feature-level registry calls and return one claim result."""

    def __init__(
        self,
        claim: ClaimResult,
        *,
        terminal_error: Exception | None = None,
    ) -> None:
        self.claim_result = claim
        self.terminal_error = terminal_error
        self.claim_calls: list[ClaimRequest] = []
        self.stored_calls: list[MarkRawStoredRequest] = []
        self.failed_calls: list[MarkFailedRequest] = []

    def claim(self, request: ClaimRequest) -> ClaimResult:
        self.claim_calls.append(request)
        return self.claim_result

    def mark_raw_stored(self, request: MarkRawStoredRequest) -> None:
        self.stored_calls.append(request)
        if self.terminal_error is not None:
            raise self.terminal_error

    def mark_failed(self, request: MarkFailedRequest) -> None:
        self.failed_calls.append(request)
        if self.terminal_error is not None:
            raise self.terminal_error


class StubDocumentSource:
    """Return one provider result or failure."""

    provider = "sec"

    def __init__(self, result: RetrievedDocument | Exception) -> None:
        self.result = result
        self.calls: list[FilingReference] = []

    def retrieve(self, filing: FilingReference) -> RetrievedDocument:
        self.calls.append(filing)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class StubRawStorage:
    """Return one S3 result or failure and record the write."""

    def __init__(self, result: StoredRawObject | Exception) -> None:
        self.result = result
        self.calls: list[RawObjectWrite] = []

    def store(self, request: RawObjectWrite) -> StoredRawObject:
        self.calls.append(request)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def filing_reference() -> FilingReference:
    """Build a discovered SEC filing."""
    return FilingReference(
        provider="sec",
        provider_filing_id="0000320193-25-000079",
        provider_issuer_id="0000320193",
        issuer_name="Apple Inc.",
        form=FilingForm.TEN_Q,
        filed_on=date(2025, 8, 1),
        report_date=None,
        accepted_at=None,
        primary_document="aapl-20250628.htm",
        filing_detail_url="https://www.sec.gov/index.htm",
        primary_document_url="https://www.sec.gov/report.htm",
    )


def request() -> AcquisitionRequest:
    """Build a deterministic acquisition invocation."""
    return AcquisitionRequest(
        filing=filing_reference(),
        owner_id="execution-1",
        requested_at=datetime(2025, 8, 1, 18, 0, tzinfo=UTC),
        lease_duration=timedelta(minutes=5),
    )


def claim_result(outcome: ClaimOutcome = ClaimOutcome.CLAIMED) -> ClaimResult:
    """Build a registry decision for the filing."""
    statuses = {
        ClaimOutcome.CLAIMED: RegistryStatus.FETCHING,
        ClaimOutcome.RECLAIMED: RegistryStatus.FETCHING,
        ClaimOutcome.ALREADY_COMPLETED: RegistryStatus.RAW_STORED,
        ClaimOutcome.ALREADY_IN_PROGRESS: RegistryStatus.FETCHING,
        ClaimOutcome.NOT_RETRYABLE: RegistryStatus.FAILED,
    }
    return ClaimResult(
        filing_key="sec#0000320193-25-000079",
        outcome=outcome,
        status=statuses[outcome],
        attempt_count=2,
        owner_id="execution-1" if outcome is ClaimOutcome.CLAIMED else None,
    )


def build_service(
    registry: StubRegistryService,
    source: StubDocumentSource,
    storage: StubRawStorage,
) -> AcquisitionService:
    """Compose concrete orchestration around feature-level test doubles."""
    return AcquisitionService(
        registry=cast(FilingRegistryService, registry),
        raw_storage=cast(S3RawDocumentClient, storage),
        sources=[cast(FilingDocumentSource, source)],
        clock=lambda: datetime(2025, 8, 1, 18, 1, tzinfo=UTC),
    )


def successful_dependencies() -> (
    tuple[StubRegistryService, StubDocumentSource, StubRawStorage]
):
    """Build dependencies for the stored path."""
    return (
        StubRegistryService(claim_result()),
        StubDocumentSource(
            RetrievedDocument(
                body=b"<html>filing</html>",
                content_type="text/html",
                source_url="https://www.sec.gov/report.htm",
                source_etag='"source"',
            )
        ),
        StubRawStorage(
            StoredRawObject(
                "filing-corpus-raw",
                "raw/sec/0000320193/accession/report.htm",
                "version-1",
                "etag-1",
                False,
            )
        ),
    )


def test_acquire_claims_retrieves_stores_and_finalizes() -> None:
    """The feature moves bytes once and returns only durable metadata."""
    registry, source, storage = successful_dependencies()

    result = build_service(registry, source, storage).acquire(request())

    assert result.outcome is AcquisitionOutcome.RAW_STORED
    assert result.document is not None
    assert result.document.sha256 == sha256(b"<html>filing</html>").hexdigest()
    assert result.document.version_id == "version-1"
    assert result.document.etag == "etag-1"
    assert storage.calls[0].key == (
        "raw/sec/0000320193/0000320193-25-000079/aapl-20250628.htm"
    )
    assert storage.calls[0].filing_key == result.filing_key
    assert registry.stored_calls[0].stored_at == datetime(2025, 8, 1, 18, 1, tzinfo=UTC)
    assert registry.failed_calls == []


@pytest.mark.parametrize(
    ("claim_outcome", "acquisition_outcome"),
    [
        (ClaimOutcome.ALREADY_COMPLETED, AcquisitionOutcome.ALREADY_COMPLETED),
        (ClaimOutcome.ALREADY_IN_PROGRESS, AcquisitionOutcome.ALREADY_IN_PROGRESS),
        (ClaimOutcome.NOT_RETRYABLE, AcquisitionOutcome.NOT_RETRYABLE),
    ],
)
def test_acquire_returns_registry_disposition_without_side_effects(
    claim_outcome: ClaimOutcome,
    acquisition_outcome: AcquisitionOutcome,
) -> None:
    """Overlapping schedules and retries do not repeat HTTP or S3 work."""
    registry, source, storage = successful_dependencies()
    registry.claim_result = claim_result(claim_outcome)

    result = build_service(registry, source, storage).acquire(request())

    assert result.outcome is acquisition_outcome
    assert source.calls == []
    assert storage.calls == []
    assert registry.stored_calls == []


@pytest.mark.parametrize(
    ("failure", "expected_type"),
    [
        (
            DocumentRetrievalError(
                "SEC returned 503",
                code="SEC_DOCUMENT_HTTP_ERROR",
                retryable=True,
            ),
            RetryableAcquisitionError,
        ),
        (
            DocumentRetrievalError(
                "bad source document",
                code="INVALID_DOCUMENT",
                retryable=False,
            ),
            PermanentAcquisitionError,
        ),
    ],
)
def test_acquire_records_provider_failures_before_raising(
    failure: DocumentRetrievalError,
    expected_type: type[Exception],
) -> None:
    """Known provider failures release the lease with explicit retry policy."""
    registry = StubRegistryService(claim_result())
    source = StubDocumentSource(failure)
    storage = StubRawStorage(RuntimeError("must not run"))

    with pytest.raises(expected_type) as raised:
        build_service(registry, source, storage).acquire(request())

    assert isinstance(
        raised.value, (RetryableAcquisitionError, PermanentAcquisitionError)
    )
    assert raised.value.code == failure.code
    assert registry.failed_calls[0].failure.retryable is failure.retryable
    assert storage.calls == []


def test_acquire_records_s3_failure_before_raising() -> None:
    """A transient object-store failure is visible and recoverable."""
    registry, source, storage = successful_dependencies()
    storage.result = RawObjectStorageError(
        "S3 unavailable",
        code="S3_SERVICEUNAVAILABLE",
        retryable=True,
    )

    with pytest.raises(RetryableAcquisitionError):
        build_service(registry, source, storage).acquire(request())

    assert registry.failed_calls[0].failure.code == "S3_SERVICEUNAVAILABLE"


def test_acquire_rejects_empty_provider_bytes_as_permanent() -> None:
    """Even a malformed provider implementation cannot store an empty object."""
    registry, _, storage = successful_dependencies()
    source = StubDocumentSource(
        RetrievedDocument(
            body=b"",
            content_type="text/html",
            source_url="https://www.sec.gov/report.htm",
        )
    )

    with pytest.raises(PermanentAcquisitionError) as raised:
        build_service(registry, source, storage).acquire(request())

    assert raised.value.code == "EMPTY_SOURCE_DOCUMENT"
    assert registry.failed_calls[0].failure.retryable is False


def test_acquire_records_an_object_key_over_the_s3_limit() -> None:
    """Oversized provider identity cannot strand a claimed filing."""
    registry, source, storage = successful_dependencies()
    oversized_request = request().model_copy(
        update={
            "filing": filing_reference().model_copy(
                update={"primary_document": "x" * 1020}
            )
        },
    )

    with pytest.raises(PermanentAcquisitionError) as raised:
        build_service(registry, source, storage).acquire(oversized_request)

    assert raised.value.code == "INVALID_RAW_OBJECT_KEY"
    assert storage.calls == []


def test_acquire_records_an_unconfigured_provider() -> None:
    """A claimed record remains diagnosable when runtime composition is incomplete."""
    registry, source, storage = successful_dependencies()
    source.provider = "other"

    with pytest.raises(PermanentAcquisitionError) as raised:
        build_service(registry, source, storage).acquire(request())

    assert raised.value.code == "UNSUPPORTED_PROVIDER"
    assert registry.failed_calls[0].failure.code == "UNSUPPORTED_PROVIDER"


def test_finalization_failure_does_not_overwrite_the_claim_as_failed() -> None:
    """After S3 succeeds, a registry retry re-verifies the same object safely."""
    registry, source, storage = successful_dependencies()
    registry.terminal_error = RuntimeError("DynamoDB unavailable")

    with pytest.raises(RuntimeError, match="DynamoDB"):
        build_service(registry, source, storage).acquire(request())

    assert len(registry.stored_calls) == 1
    assert registry.failed_calls == []


def test_unexpected_provider_bug_leaves_the_lease_for_recovery() -> None:
    """Unknown programming errors are not mislabeled as source-data failures."""
    registry = StubRegistryService(claim_result())
    source = StubDocumentSource(RuntimeError("bug"))
    storage = StubRawStorage(RuntimeError("must not run"))

    with pytest.raises(RuntimeError, match="bug"):
        build_service(registry, source, storage).acquire(request())

    assert registry.failed_calls == []


def test_service_rejects_missing_or_duplicate_sources() -> None:
    """Provider routing ambiguity fails during runtime composition."""
    registry, source, storage = successful_dependencies()
    with pytest.raises(ValueError, match="at least one"):
        AcquisitionService(
            registry=cast(FilingRegistryService, registry),
            raw_storage=cast(S3RawDocumentClient, storage),
            sources=[],
        )
    with pytest.raises(ValueError, match="duplicate"):
        AcquisitionService(
            registry=cast(FilingRegistryService, registry),
            raw_storage=cast(S3RawDocumentClient, storage),
            sources=[
                cast(FilingDocumentSource, source),
                cast(FilingDocumentSource, source),
            ],
        )
