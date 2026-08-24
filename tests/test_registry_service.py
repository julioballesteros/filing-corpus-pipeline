"""Tests for filing registry state policy independent of DynamoDB syntax."""

from datetime import UTC, date, datetime, timedelta
from typing import cast

import pytest

from filing_corpus_pipeline.domain import FilingForm, FilingReference, IssuerReference
from filing_corpus_pipeline.registry import (
    ClaimOutcome,
    ClaimRequest,
    FailureDetails,
    FilingRegistryError,
    FilingRegistryService,
    InvalidRegistryItemError,
    MarkFailedRequest,
    MarkRawStoredRequest,
    RawDocumentMetadata,
    RegistryConsistencyError,
    RegistryLeaseLostError,
    RegistryStatus,
)
from filing_corpus_pipeline.storage import (
    ConditionalWriteFailed,
    DynamoDbRegistryClient,
    DynamoDbStorageError,
    InvalidDynamoDbItemError,
    StoredRegistryItem,
)


class StubRegistryClient:
    """Scripted semantic storage client used to isolate service policy."""

    def __init__(
        self,
        *,
        claims: list[StoredRegistryItem | Exception | None] | None = None,
        reads: list[StoredRegistryItem | Exception | None] | None = None,
        terminal_error: Exception | None = None,
    ) -> None:
        self.claims = list(claims or [None])
        self.reads = list(reads or [])
        self.terminal_error = terminal_error
        self.claim_calls: list[ClaimRequest] = []
        self.stored_calls: list[MarkRawStoredRequest] = []
        self.failed_calls: list[MarkFailedRequest] = []

    def claim(self, request: ClaimRequest) -> StoredRegistryItem | None:
        self.claim_calls.append(request)
        result = self.claims.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def get(self, filing_key: str) -> StoredRegistryItem | None:
        del filing_key
        result = self.reads.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def mark_raw_stored(self, request: MarkRawStoredRequest) -> None:
        self.stored_calls.append(request)
        if self.terminal_error is not None:
            raise self.terminal_error

    def mark_failed(self, request: MarkFailedRequest) -> None:
        self.failed_calls.append(request)
        if self.terminal_error is not None:
            raise self.terminal_error


def service(client: StubRegistryClient) -> FilingRegistryService:
    """Build the concrete service around the scripted storage client."""
    return FilingRegistryService(cast(DynamoDbRegistryClient, client))


def filing_reference() -> FilingReference:
    """Build the filing passed from discovery to acquisition."""
    return FilingReference(
        provider="sec",
        provider_filing_id="0000320193-25-000079",
        issuer=IssuerReference("sec", "0000320193"),
        issuer_name="Apple Inc.",
        form=FilingForm.TEN_Q,
        filed_on=date(2025, 8, 1),
        report_date=None,
        accepted_at=None,
        primary_document="aapl-20250628.htm",
        filing_detail_url="https://example.test/filing-index.html",
        primary_document_url="https://example.test/aapl-20250628.htm",
    )


def claim_request() -> ClaimRequest:
    """Build a deterministic five-minute claim."""
    return ClaimRequest(
        filing=filing_reference(),
        owner_id="execution-1",
        claimed_at=datetime(2025, 8, 1, 18, 0, tzinfo=UTC),
        lease_duration=timedelta(minutes=5),
    )


def stored_item(
    status: str,
    *,
    attempts: int = 1,
    owner: str | None = None,
    retryable: bool | None = None,
) -> StoredRegistryItem:
    """Build the state returned by the storage client."""
    return StoredRegistryItem(status, attempts, owner, retryable)


def test_claim_reports_first_and_recovered_work() -> None:
    """The service distinguishes a new claim from an existing item recovery."""
    first = service(StubRegistryClient()).claim(claim_request())
    recovered = service(
        StubRegistryClient(claims=[stored_item("FAILED", attempts=2, retryable=True)])
    ).claim(claim_request())

    assert first.outcome is ClaimOutcome.CLAIMED
    assert first.attempt_count == 1
    assert first.owner_id == "execution-1"
    assert recovered.outcome is ClaimOutcome.RECLAIMED
    assert recovered.attempt_count == 3


@pytest.mark.parametrize(
    ("current", "outcome", "status", "owner"),
    [
        (
            stored_item("RAW_STORED", attempts=2),
            ClaimOutcome.ALREADY_COMPLETED,
            RegistryStatus.RAW_STORED,
            None,
        ),
        (
            stored_item("FETCHING", owner="execution-other"),
            ClaimOutcome.ALREADY_IN_PROGRESS,
            RegistryStatus.FETCHING,
            "execution-other",
        ),
        (
            stored_item("FAILED", retryable=False),
            ClaimOutcome.NOT_RETRYABLE,
            RegistryStatus.FAILED,
            None,
        ),
    ],
)
def test_claim_classifies_conditional_conflicts(
    current: StoredRegistryItem,
    outcome: ClaimOutcome,
    status: RegistryStatus,
    owner: str | None,
) -> None:
    """Duplicate and active-work policy is independent of DynamoDB syntax."""
    client = StubRegistryClient(
        claims=[ConditionalWriteFailed("conflict")],
        reads=[current],
    )

    result = service(client).claim(claim_request())

    assert result.outcome is outcome
    assert result.status is status
    assert result.owner_id == owner
    assert result.acquired is False


def test_claim_retries_a_racing_retryable_failure_once() -> None:
    """A retryable state observed during a conflict remains recoverable."""
    client = StubRegistryClient(
        claims=[
            ConditionalWriteFailed("conflict"),
            stored_item("FAILED", attempts=1, retryable=True),
        ],
        reads=[stored_item("FAILED", attempts=1, retryable=True)],
    )

    result = service(client).claim(claim_request())

    assert result.outcome is ClaimOutcome.RECLAIMED
    assert result.attempt_count == 2
    assert len(client.claim_calls) == 2


def test_claim_reports_an_unstable_conflict() -> None:
    """Repeated conflicts without visible state fail explicitly."""
    client = StubRegistryClient(
        claims=[ConditionalWriteFailed("one"), ConditionalWriteFailed("two")],
        reads=[None, None],
    )

    with pytest.raises(RegistryConsistencyError):
        service(client).claim(claim_request())


@pytest.mark.parametrize(
    "current",
    [
        stored_item("UNKNOWN"),
        stored_item("FAILED", retryable=None),
        stored_item("FETCHING", owner=None),
    ],
)
def test_claim_rejects_invalid_feature_state(current: StoredRegistryItem) -> None:
    """Unknown or incomplete state cannot silently become a duplicate."""
    client = StubRegistryClient(
        claims=[ConditionalWriteFailed("conflict")],
        reads=[current],
    )

    with pytest.raises(InvalidRegistryItemError):
        service(client).claim(claim_request())


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (DynamoDbStorageError("offline"), FilingRegistryError),
        (InvalidDynamoDbItemError("bad item"), InvalidRegistryItemError),
    ],
)
def test_claim_translates_storage_failures(
    error: Exception,
    expected: type[Exception],
) -> None:
    """Storage errors are expressed in registry feature terms."""
    with pytest.raises(expected):
        service(StubRegistryClient(claims=[error])).claim(claim_request())


def raw_stored_request() -> MarkRawStoredRequest:
    """Build a completed acquisition transition."""
    return MarkRawStoredRequest(
        filing_key="sec#filing",
        owner_id="execution-1",
        stored_at=datetime(2025, 8, 1, tzinfo=UTC),
        document=RawDocumentMetadata(
            "bucket",
            "raw/filing.htm",
            "a" * 64,
            10,
            "text/html",
        ),
    )


def failed_request() -> MarkFailedRequest:
    """Build a failed acquisition transition."""
    return MarkFailedRequest(
        filing_key="sec#filing",
        owner_id="execution-1",
        failed_at=datetime(2025, 8, 1, tzinfo=UTC),
        failure=FailureDetails("ERROR", "failed", True),
    )


def test_terminal_transitions_delegate_to_storage() -> None:
    """The service keeps state policy while the client owns persistence."""
    client = StubRegistryClient()
    registry = service(client)

    registry.mark_raw_stored(raw_stored_request())
    registry.mark_failed(failed_request())

    assert client.stored_calls == [raw_stored_request()]
    assert client.failed_calls == [failed_request()]


@pytest.mark.parametrize("operation", ["stored", "failed"])
def test_terminal_transitions_reject_a_lost_claim(operation: str) -> None:
    """A late worker cannot overwrite a newer owner's state."""
    registry = service(
        StubRegistryClient(terminal_error=ConditionalWriteFailed("conflict"))
    )

    with pytest.raises(RegistryLeaseLostError):
        if operation == "stored":
            registry.mark_raw_stored(raw_stored_request())
        else:
            registry.mark_failed(failed_request())


def test_terminal_transition_translates_storage_failure() -> None:
    """Nonconditional write failures remain operational registry errors."""
    registry = service(StubRegistryClient(terminal_error=DynamoDbStorageError("down")))

    with pytest.raises(FilingRegistryError, match="storage update"):
        registry.mark_failed(failed_request())
