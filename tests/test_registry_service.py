"""Tests for filing registry state policy independent of DynamoDB syntax."""

from datetime import UTC, date, datetime, timedelta
from typing import cast

import pytest

from filing_corpus_pipeline.domain import FilingForm, FilingReference
from filing_corpus_pipeline.registry import (
    ClaimOutcome,
    ClaimRequest,
    FailureDetails,
    FilingRegistryError,
    FilingRegistryService,
    InvalidRegistryItemError,
    MarkFailedRequest,
    MarkNormalizationFailedRequest,
    MarkNormalizedRequest,
    MarkRawStoredRequest,
    NormalizationClaimRequest,
    NormalizedCorpusMetadata,
    RawDocumentMetadata,
    RegistryConsistencyError,
    RegistryLeaseLostError,
    RegistryStatus,
)
from filing_corpus_pipeline.storage.dynamodb import (
    ConditionalWriteFailed,
    DynamoDbRegistryClient,
    DynamoDbStorageError,
    InvalidDynamoDbItemError,
    StoredNormalizationItem,
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
        normalization_claims: list[StoredNormalizationItem | Exception] | None = None,
        normalization_reads: (
            list[StoredNormalizationItem | Exception | None] | None
        ) = None,
    ) -> None:
        self.claims = list(claims or [None])
        self.reads = list(reads or [])
        self.terminal_error = terminal_error
        self.normalization_claims = list(normalization_claims or [])
        self.normalization_reads = list(normalization_reads or [])
        self.claim_calls: list[ClaimRequest] = []
        self.stored_calls: list[MarkRawStoredRequest] = []
        self.failed_calls: list[MarkFailedRequest] = []
        self.normalized_calls: list[MarkNormalizedRequest] = []
        self.normalization_failed_calls: list[MarkNormalizationFailedRequest] = []

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

    def claim_normalization(
        self, request: NormalizationClaimRequest
    ) -> StoredNormalizationItem:
        del request
        result = self.normalization_claims.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def get_normalization(self, filing_key: str) -> StoredNormalizationItem | None:
        del filing_key
        result = self.normalization_reads.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def mark_normalized(self, request: MarkNormalizedRequest) -> None:
        self.normalized_calls.append(request)
        if self.terminal_error is not None:
            raise self.terminal_error

    def mark_normalization_failed(
        self, request: MarkNormalizationFailedRequest
    ) -> None:
        self.normalization_failed_calls.append(request)
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
        provider_issuer_id="0000320193",
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
            stored_item("NORMALIZING", attempts=2),
            ClaimOutcome.ALREADY_COMPLETED,
            RegistryStatus.NORMALIZING,
            None,
        ),
        (
            stored_item("NORMALIZED", attempts=2),
            ClaimOutcome.ALREADY_COMPLETED,
            RegistryStatus.NORMALIZED,
            None,
        ),
        (
            stored_item("NORMALIZATION_FAILED", attempts=2),
            ClaimOutcome.ALREADY_COMPLETED,
            RegistryStatus.NORMALIZATION_FAILED,
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
            bucket="bucket",
            key="raw/filing.htm",
            sha256="a" * 64,
            content_length=10,
            content_type="text/html",
        ),
    )


def failed_request() -> MarkFailedRequest:
    """Build a failed acquisition transition."""
    return MarkFailedRequest(
        filing_key="sec#filing",
        owner_id="execution-1",
        failed_at=datetime(2025, 8, 1, tzinfo=UTC),
        failure=FailureDetails(code="ERROR", message="failed", retryable=True),
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


def normalization_request(
    parser_version: str = "sec-html-v2",
) -> NormalizationClaimRequest:
    return NormalizationClaimRequest(
        filing_key="sec#filing",
        owner_id="execution-1",
        claimed_at=datetime(2025, 8, 1, tzinfo=UTC),
        lease_duration=timedelta(minutes=10),
        parser_version=parser_version,
    )


def normalized_corpus() -> NormalizedCorpusMetadata:
    return NormalizedCorpusMetadata(
        bucket="normalized",
        prefix="normalized/prefix",
        manifest_key="normalized/prefix/manifest.json",
        manifest_sha256="a" * 64,
        blocks_key="normalized/prefix/blocks.jsonl.gz",
        blocks_sha256="b" * 64,
        parser_version="sec-html-v2",
        schema_version="1",
        block_count=10,
        section_count=2,
        warning_count=0,
        quality_status="PASS",
    )


def normalization_item(
    status: str,
    *,
    attempts: int = 0,
    parser_version: str | None = None,
    owner: str | None = None,
    retryable: bool | None = None,
    corpus: NormalizedCorpusMetadata | None = None,
) -> StoredNormalizationItem:
    return StoredNormalizationItem(
        status=status,
        attempt_count=attempts,
        raw_document=raw_stored_request().document,
        parser_version=parser_version,
        owner_id=owner,
        retryable=retryable,
        corpus=corpus,
    )


def test_normalization_claim_reports_first_and_recovered_work() -> None:
    first = service(
        StubRegistryClient(normalization_claims=[normalization_item("RAW_STORED")])
    ).claim_normalization(normalization_request())
    recovered = service(
        StubRegistryClient(
            normalization_claims=[
                normalization_item(
                    "NORMALIZATION_FAILED",
                    attempts=2,
                    parser_version="sec-html-v2",
                    retryable=True,
                )
            ]
        )
    ).claim_normalization(normalization_request())

    assert first.outcome is ClaimOutcome.CLAIMED
    assert first.status is RegistryStatus.NORMALIZING
    assert first.attempt_count == 1
    assert recovered.outcome is ClaimOutcome.RECLAIMED
    assert recovered.attempt_count == 3


@pytest.mark.parametrize(
    ("current", "outcome"),
    [
        (
            normalization_item(
                "NORMALIZED",
                attempts=1,
                parser_version="sec-html-v2",
                corpus=normalized_corpus(),
            ),
            ClaimOutcome.ALREADY_COMPLETED,
        ),
        (
            normalization_item("NORMALIZING", attempts=1, owner="other"),
            ClaimOutcome.ALREADY_IN_PROGRESS,
        ),
        (
            normalization_item(
                "NORMALIZATION_FAILED",
                attempts=1,
                parser_version="sec-html-v2",
                retryable=False,
            ),
            ClaimOutcome.NOT_RETRYABLE,
        ),
    ],
)
def test_normalization_claim_classifies_duplicate_states(
    current: StoredNormalizationItem,
    outcome: ClaimOutcome,
) -> None:
    registry = service(
        StubRegistryClient(
            normalization_claims=[ConditionalWriteFailed("conflict")],
            normalization_reads=[current],
        )
    )

    result = registry.claim_normalization(normalization_request())

    assert result.outcome is outcome
    assert result.acquired is False


def test_new_parser_version_reclaims_a_previous_normalized_item() -> None:
    old = normalization_item(
        "NORMALIZED",
        attempts=1,
        parser_version="sec-html-v1",
        corpus=normalized_corpus(),
    )
    client = StubRegistryClient(
        normalization_claims=[ConditionalWriteFailed("race"), old],
        normalization_reads=[old],
    )

    result = service(client).claim_normalization(normalization_request())

    assert result.outcome is ClaimOutcome.RECLAIMED
    assert result.attempt_count == 2


def test_normalization_terminal_transitions_delegate_and_protect_the_lease() -> None:
    corpus = normalized_corpus()
    normalized = MarkNormalizedRequest(
        filing_key="sec#filing",
        owner_id="execution-1",
        normalized_at=datetime.now(UTC),
        corpus=corpus,
    )
    failed = MarkNormalizationFailedRequest(
        filing_key="sec#filing",
        owner_id="execution-1",
        failed_at=datetime.now(UTC),
        parser_version="sec-html-v2",
        failure=FailureDetails(
            code="PARSE",
            message="bad filing",
            retryable=False,
        ),
    )
    client = StubRegistryClient()
    registry = service(client)

    registry.mark_normalized(normalized)
    registry.mark_normalization_failed(failed)

    assert client.normalized_calls == [normalized]
    assert client.normalization_failed_calls == [failed]

    lost = service(StubRegistryClient(terminal_error=ConditionalWriteFailed("lost")))
    with pytest.raises(RegistryLeaseLostError):
        lost.mark_normalized(normalized)
