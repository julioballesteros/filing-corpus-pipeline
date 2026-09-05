"""Tests for provider-independent discovery behaviour."""

from datetime import UTC, date, datetime

import pytest

from filing_corpus_pipeline.discovery.models import DiscoveryRequest
from filing_corpus_pipeline.discovery.service import (
    ConflictingFilingReferenceError,
    DiscoveryService,
)
from filing_corpus_pipeline.domain import FilingForm, FilingReference, IssuerReference


class StubSource:
    """In-memory discovery source used to exercise the application service."""

    provider = "sec"

    def __init__(self, filings: list[FilingReference]) -> None:
        self.filings = filings

    def discover(self, request: DiscoveryRequest) -> list[FilingReference]:
        del request
        return self.filings


def filing_reference(
    accession: str,
    *,
    filed_on: date = date(2025, 1, 1),
    issuer_name: str = "Example Corp",
) -> FilingReference:
    """Build a valid filing reference with concise defaults."""
    return FilingReference(
        provider="sec",
        provider_filing_id=accession,
        provider_issuer_id="0000320193",
        issuer_name=issuer_name,
        form=FilingForm.TEN_K,
        filed_on=filed_on,
        report_date=date(2024, 12, 31),
        accepted_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
        primary_document="filing.htm",
        filing_detail_url=f"https://example.test/{accession}-index.html",
        primary_document_url="https://example.test/filing.htm",
    )


def discovery_request(*, provider: str = "sec") -> DiscoveryRequest:
    """Build a request covering both initial filing forms."""
    return DiscoveryRequest(
        issuers=(IssuerReference(provider=provider, provider_issuer_id="320193"),),
        forms=frozenset(FilingForm),
        filed_from=date(2024, 1, 1),
        filed_to=date(2026, 1, 1),
    )


def test_service_deduplicates_and_sorts_filing_references() -> None:
    """Provider overlap must not produce unstable workflow input."""
    later = filing_reference("0000320193-25-000002", filed_on=date(2025, 3, 1))
    earlier = filing_reference("0000320193-25-000001", filed_on=date(2025, 2, 1))

    result = DiscoveryService(StubSource([later, earlier, earlier])).execute(
        discovery_request()
    )

    assert result.filings == (earlier, later)
    assert result.issuers_scanned == 1
    payload = result.model_dump(mode="json")
    assert payload["filings_found"] == 2
    assert payload["filings"][0] == earlier.model_dump(mode="json")


def test_service_aggregates_registration_specific_requests() -> None:
    """Per-registration form selections share one deterministic workflow result."""
    filing = filing_reference("0000320193-25-000001")
    service = DiscoveryService(StubSource([filing]))
    first = discovery_request()
    second = discovery_request().model_copy(
        update={
            "issuers": (IssuerReference(provider="sec", provider_issuer_id="789019"),),
            "forms": frozenset({FilingForm.TEN_Q}),
        }
    )

    result = service.execute_many((first, second))

    assert result.filings == (filing,)
    assert result.issuers_scanned == 2


def test_service_rejects_conflicting_duplicate_references() -> None:
    """The same provider ID cannot silently describe two different filings."""
    original = filing_reference("0000320193-25-000001")
    conflicting = filing_reference(
        "0000320193-25-000001",
        issuer_name="Unexpected Name",
    )

    with pytest.raises(ConflictingFilingReferenceError):
        DiscoveryService(StubSource([original, conflicting])).execute(
            discovery_request()
        )


def test_service_rejects_issuer_from_another_provider() -> None:
    """A source adapter may only receive issuer IDs in its own namespace."""
    with pytest.raises(ValueError, match="cannot discover"):
        DiscoveryService(StubSource([])).execute(discovery_request(provider="other"))


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"issuers": ()}, "issuers"),
        ({"forms": frozenset()}, "forms"),
        (
            {"filed_from": date(2026, 1, 2), "filed_to": date(2026, 1, 1)},
            "filed_from",
        ),
    ],
)
def test_discovery_request_validates_its_bounds(
    overrides: dict[str, object],
    message: str,
) -> None:
    """Invalid workflow input fails before any provider request is made."""
    values: dict[str, object] = {
        "issuers": (IssuerReference(provider="sec", provider_issuer_id="320193"),),
        "forms": frozenset(FilingForm),
        "filed_from": date(2024, 1, 1),
        "filed_to": date(2026, 1, 1),
    }
    values.update(overrides)

    with pytest.raises(ValueError, match=message):
        DiscoveryRequest(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("provider", "provider_issuer_id"),
    [("", "320193"), ("sec", "")],
)
def test_issuer_reference_requires_nonempty_identity(
    provider: str,
    provider_issuer_id: str,
) -> None:
    """Empty provider identities cannot become idempotency keys."""
    with pytest.raises(ValueError):
        IssuerReference(provider=provider, provider_issuer_id=provider_issuer_id)


def test_filing_reference_derives_matching_issuer_identity() -> None:
    """The issuer view cannot diverge from the filing provider namespace."""
    valid = filing_reference("0000320193-25-000001")

    assert valid.issuer == IssuerReference(
        provider="sec",
        provider_issuer_id="0000320193",
    )


def test_filing_reference_round_trips_its_workflow_contract() -> None:
    """Acquisition reconstructs the exact provider-neutral discovery record."""
    filing = filing_reference("0000320193-25-000001")

    payload = filing.model_dump(mode="json")

    assert FilingReference.model_validate(payload) == filing


def test_filing_reference_contract_is_frozen_and_rejects_unknown_fields() -> None:
    """Boundary records cannot drift or mutate after validation."""
    filing = filing_reference("0000320193-25-000001")
    payload = filing.model_dump(mode="json")
    payload["unexpected"] = "schema drift"

    with pytest.raises(ValueError, match="unexpected"):
        FilingReference.model_validate(payload)
    with pytest.raises(ValueError, match="frozen"):
        filing.issuer_name = "Changed"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"filed_on": "bad-date"}, "filed_on"),
        ({"report_date": 1}, "report_date"),
        ({"report_date": "bad"}, "report_date"),
        ({"accepted_at": 1}, "accepted_at"),
        ({"accepted_at": "bad"}, "accepted_at"),
        (
            {"accepted_at": "2025-01-01T12:00:00"},
            "accepted_at",
        ),
    ],
)
def test_filing_reference_parser_rejects_invalid_temporal_fields(
    overrides: dict[str, object],
    message: str,
) -> None:
    """Schema drift in a workflow record is rejected at the handoff boundary."""
    payload: dict[str, object] = {
        **filing_reference("0000320193-25-000001").model_dump(mode="json")
    }
    payload.update(overrides)

    with pytest.raises(ValueError, match=message):
        FilingReference.model_validate(payload)
