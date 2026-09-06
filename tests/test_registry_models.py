"""Tests for provider-neutral filing registry values."""

from datetime import UTC, date, datetime, timedelta

import pytest

from filing_corpus_pipeline.domain import FilingReference
from filing_corpus_pipeline.registry import (
    ClaimOutcome,
    ClaimRequest,
    ClaimResult,
    FailureDetails,
    MarkFailedRequest,
    MarkRawStoredRequest,
    RawDocumentMetadata,
    RegistryStatus,
    filing_registry_key,
)


def filing_reference() -> FilingReference:
    """Build a representative provider-neutral filing reference."""
    return FilingReference(
        company_id="apple-inc",
        provider="sec",
        provider_filing_id="0000320193-25-000079",
        provider_issuer_id="0000320193",
        issuer_name="Apple Inc.",
        filing_type="10-Q",
        filed_on=date(2025, 8, 1),
        report_date=date(2025, 6, 28),
        accepted_at=datetime(2025, 8, 1, 16, 30, tzinfo=UTC),
        primary_document="aapl-20250628.htm",
        filing_detail_url="https://example.test/filing-index.html",
        primary_document_url="https://example.test/aapl-20250628.htm",
    )


def raw_document() -> RawDocumentMetadata:
    """Build valid raw-object metadata."""
    return RawDocumentMetadata(
        bucket="filing-corpus-raw",
        key="raw/sec/0000320193/filing.htm",
        sha256="a" * 64,
        content_length=1024,
        content_type="text/html",
    )


def test_registry_key_namespaces_and_escapes_provider_ids() -> None:
    """Delimiter characters cannot create collisions between provider keys."""
    assert filing_registry_key("provider#one", "filing / 1") == (
        "provider%23one#filing%20%2F%201"
    )


@pytest.mark.parametrize(
    ("provider", "filing_id"),
    [("", "filing"), ("sec", "")],
)
def test_registry_key_rejects_empty_identity(provider: str, filing_id: str) -> None:
    """Every registry item must have a complete provider identity."""
    with pytest.raises(ValueError):
        filing_registry_key(provider, filing_id)


def test_claim_request_derives_key_and_utc_lease_expiry() -> None:
    """Claims expose the stable key and a timezone-normalized lease boundary."""
    request = ClaimRequest(
        filing=filing_reference(),
        owner_id="execution-1",
        claimed_at=datetime(2025, 8, 1, 18, 0, tzinfo=UTC),
        lease_duration=timedelta(minutes=5),
    )

    assert request.filing_key == "sec#0000320193-25-000079"
    assert request.lease_expires_at == datetime(2025, 8, 1, 18, 5, tzinfo=UTC)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"owner_id": ""}, "owner_id"),
        ({"claimed_at": datetime(2025, 1, 1)}, "timezone"),
        ({"lease_duration": timedelta(0)}, "lease_duration"),
        ({"lease_duration": timedelta(days=2)}, "lease_duration"),
    ],
)
def test_claim_request_rejects_unsafe_lease_values(
    overrides: dict[str, object],
    message: str,
) -> None:
    """Invalid leases fail before any persistence call."""
    values: dict[str, object] = {
        "filing": filing_reference(),
        "owner_id": "execution-1",
        "claimed_at": datetime(2025, 8, 1, tzinfo=UTC),
        "lease_duration": timedelta(minutes=5),
    }
    values.update(overrides)

    with pytest.raises(ValueError, match=message):
        ClaimRequest(**values)  # type: ignore[arg-type]


def test_claim_result_reports_ownership() -> None:
    """Only successful claim outcomes authorize acquisition work."""
    claimed = ClaimResult(
        filing_key="sec#filing",
        outcome=ClaimOutcome.CLAIMED,
        status=RegistryStatus.FETCHING,
        attempt_count=1,
        owner_id="execution-1",
    )
    duplicate = claimed.model_copy(
        update={
            "outcome": ClaimOutcome.ALREADY_COMPLETED,
            "status": RegistryStatus.RAW_STORED,
            "owner_id": None,
        },
    )

    assert claimed.acquired is True
    assert duplicate.acquired is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"sha256": "not-a-digest"},
        {"content_length": -1},
        {"bucket": ""},
        {"key": ""},
        {"content_type": ""},
        {"version_id": ""},
        {"etag": ""},
    ],
)
def test_raw_document_metadata_validates_integrity_fields(
    overrides: dict[str, object],
) -> None:
    """Incomplete object metadata cannot mark a filing as durable."""
    values: dict[str, object] = {
        "bucket": "filing-corpus-raw",
        "key": "raw/filing.htm",
        "sha256": "a" * 64,
        "content_length": 10,
        "content_type": "text/html",
    }
    values.update(overrides)

    with pytest.raises(ValueError):
        RawDocumentMetadata(**values)  # type: ignore[arg-type]


def test_transition_requests_require_aware_timestamps() -> None:
    """Registry audit timestamps must identify an absolute instant."""
    with pytest.raises(ValueError, match="timezone"):
        MarkRawStoredRequest(
            filing_key="sec#filing",
            owner_id="execution-1",
            stored_at=datetime(2025, 1, 1),
            document=raw_document(),
        )

    with pytest.raises(ValueError, match="timezone"):
        MarkFailedRequest(
            filing_key="sec#filing",
            owner_id="execution-1",
            failed_at=datetime(2025, 1, 1),
            failure=FailureDetails(
                code="HTTP_ERROR",
                message="request failed",
                retryable=True,
            ),
        )


@pytest.mark.parametrize(
    ("code", "message"),
    [("", "failed"), ("HTTP_ERROR", ""), ("x" * 101, "failed"), ("ERR", "x" * 1001)],
)
def test_failure_details_are_bounded(code: str, message: str) -> None:
    """Unbounded provider errors cannot consume an entire DynamoDB item."""
    with pytest.raises(ValueError):
        FailureDetails(code=code, message=message, retryable=True)
