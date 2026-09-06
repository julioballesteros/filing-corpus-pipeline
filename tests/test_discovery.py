"""Tests for source-neutral discovery orchestration and contracts."""

from datetime import UTC, date, datetime

import pytest

from filing_corpus_pipeline.discovery.models import DiscoveryRequest, DiscoveryTarget
from filing_corpus_pipeline.discovery.service import (
    ConflictingFilingReferenceError,
    DiscoveryService,
    InvalidDiscoverySourceResultError,
    UnsupportedDiscoverySourceError,
)
from filing_corpus_pipeline.domain import (
    DocumentPolicy,
    FilingReference,
    FilingSelection,
    IssuerReference,
)


class StubSource:
    """In-memory discovery source used to exercise application routing."""

    def __init__(
        self,
        provider: str,
        filings: list[FilingReference],
        *,
        rejected_company: str | None = None,
    ) -> None:
        self.provider = provider
        self.filings = filings
        self.rejected_company = rejected_company
        self.validated: list[DiscoveryTarget] = []
        self.requests: list[DiscoveryRequest] = []

    def validate_target(self, target: DiscoveryTarget) -> None:
        self.validated.append(target)
        if target.company_id == self.rejected_company:
            raise ValueError(f"unsupported target: {target.company_id}")

    def discover(self, request: DiscoveryRequest) -> list[FilingReference]:
        self.requests.append(request)
        return self.filings


def filing_reference(
    filing_id: str,
    *,
    provider: str = "sec",
    company_id: str = "example-corp",
    filing_type: str = "10-K",
    filed_on: date = date(2025, 1, 1),
    issuer_name: str = "Example Corp",
) -> FilingReference:
    """Build a valid filing reference with concise defaults."""
    return FilingReference(
        company_id=company_id,
        provider=provider,
        provider_filing_id=filing_id,
        provider_issuer_id="issuer-1",
        issuer_name=issuer_name,
        filing_type=filing_type,
        document_policy=DocumentPolicy.PRIMARY,
        filed_on=filed_on,
        report_date=date(2024, 12, 31),
        accepted_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
        primary_document="filing.htm",
        filing_detail_url=f"https://example.test/{filing_id}-index.html",
        primary_document_url="https://example.test/filing.htm",
    )


def discovery_target(
    *,
    provider: str = "sec",
    company_id: str = "example-corp",
    issuer_id: str = "issuer-1",
    filing_types: tuple[str, ...] = ("10-K", "10-Q"),
) -> DiscoveryTarget:
    return DiscoveryTarget(
        company_id=company_id,
        issuer=IssuerReference(
            provider=provider,
            provider_issuer_id=issuer_id,
        ),
        selections=tuple(
            FilingSelection(filing_type=filing_type) for filing_type in filing_types
        ),
    )


def discovery_request(*targets: DiscoveryTarget) -> DiscoveryRequest:
    """Build a bounded request for one or more source targets."""
    return DiscoveryRequest(
        targets=targets or (discovery_target(),),
        filed_from=date(2024, 1, 1),
        filed_to=date(2026, 1, 1),
    )


def test_service_deduplicates_and_sorts_filing_references() -> None:
    """Provider overlap must not produce unstable workflow input."""
    later = filing_reference("filing-2", filed_on=date(2025, 3, 1))
    earlier = filing_reference("filing-1", filed_on=date(2025, 2, 1))

    result = DiscoveryService([StubSource("sec", [later, earlier, earlier])]).execute(
        discovery_request()
    )

    assert result.filings == (earlier, later)
    assert result.issuers_scanned == 1
    payload = result.model_dump(mode="json")
    assert payload["filings_found"] == 2
    assert payload["filings"][0] == earlier.model_dump(mode="json")


def test_service_routes_each_provider_as_one_homogeneous_request() -> None:
    """Sources receive only their targets while results share one stable output."""
    sec_filing = filing_reference("sec-filing")
    other_filing = filing_reference(
        "other-filing",
        provider="other",
        company_id="other-corp",
        filing_type="annual-report",
    )
    sec = StubSource("sec", [sec_filing])
    other = StubSource("other", [other_filing])
    request = discovery_request(
        discovery_target(),
        discovery_target(
            provider="other",
            company_id="other-corp",
            issuer_id="other-issuer",
            filing_types=("annual-report",),
        ),
    )

    result = DiscoveryService([other, sec]).execute(request)

    assert result.filings == (other_filing, sec_filing)
    assert result.issuers_scanned == 2
    assert [target.issuer.provider for target in sec.requests[0].targets] == ["sec"]
    assert [target.issuer.provider for target in other.requests[0].targets] == ["other"]


def test_service_validates_every_target_before_calling_any_source() -> None:
    """An invalid deployed route cannot create a partial discovery run."""
    sec = StubSource("sec", [], rejected_company="invalid-corp")
    other = StubSource("other", [])
    request = discovery_request(
        discovery_target(
            provider="other",
            company_id="other-corp",
            issuer_id="other-issuer",
            filing_types=("annual-report",),
        ),
        discovery_target(company_id="invalid-corp", issuer_id="invalid-issuer"),
    )

    with pytest.raises(ValueError, match="unsupported target"):
        DiscoveryService([sec, other]).execute(request)

    assert sec.requests == []
    assert other.requests == []


def test_service_rejects_an_unregistered_source() -> None:
    with pytest.raises(UnsupportedDiscoverySourceError, match="other"):
        DiscoveryService([StubSource("sec", [])]).execute(
            discovery_request(
                discovery_target(
                    provider="other",
                    company_id="other-corp",
                    issuer_id="other-issuer",
                )
            )
        )


@pytest.mark.parametrize(
    "sources",
    [[], [StubSource("", [])], [StubSource("sec", []), StubSource("sec", [])]],
)
def test_service_requires_unique_nonempty_sources(sources: list[StubSource]) -> None:
    with pytest.raises(ValueError):
        DiscoveryService(sources)


def test_service_rejects_conflicting_duplicate_references() -> None:
    original = filing_reference("filing-1")
    conflicting = filing_reference("filing-1", issuer_name="Unexpected Name")

    with pytest.raises(ConflictingFilingReferenceError):
        DiscoveryService([StubSource("sec", [original, conflicting])]).execute(
            discovery_request()
        )


def test_service_rejects_a_result_from_the_wrong_provider() -> None:
    with pytest.raises(InvalidDiscoverySourceResultError, match="returned provider"):
        DiscoveryService(
            [StubSource("sec", [filing_reference("filing-1", provider="other")])]
        ).execute(discovery_request())


def test_service_rejects_an_unrequested_result_selection() -> None:
    with pytest.raises(InvalidDiscoverySourceResultError, match="unrequested"):
        DiscoveryService(
            [StubSource("sec", [filing_reference("filing-1", filing_type="8-K")])]
        ).execute(discovery_request())


def test_service_rejects_a_result_outside_the_requested_window() -> None:
    with pytest.raises(InvalidDiscoverySourceResultError, match="outside"):
        DiscoveryService(
            [
                StubSource(
                    "sec",
                    [filing_reference("filing-1", filed_on=date(2027, 1, 1))],
                )
            ]
        ).execute(discovery_request())


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"targets": ()}, "targets"),
        (
            {"filed_from": date(2026, 1, 2), "filed_to": date(2026, 1, 1)},
            "filed_from",
        ),
        (
            {
                "targets": (
                    discovery_target(),
                    discovery_target(company_id="duplicate-company"),
                )
            },
            "unique provider/issuer",
        ),
    ],
)
def test_discovery_request_validates_its_bounds(
    overrides: dict[str, object],
    message: str,
) -> None:
    values: dict[str, object] = {
        "targets": (discovery_target(),),
        "filed_from": date(2024, 1, 1),
        "filed_to": date(2026, 1, 1),
    }
    values.update(overrides)

    with pytest.raises(ValueError, match=message):
        DiscoveryRequest(**values)  # type: ignore[arg-type]


def test_discovery_target_rejects_duplicate_filing_types() -> None:
    with pytest.raises(ValueError, match="unique filing_type"):
        DiscoveryTarget(
            company_id="example-corp",
            issuer=IssuerReference(provider="sec", provider_issuer_id="issuer-1"),
            selections=(
                FilingSelection(filing_type="10-K"),
                FilingSelection(filing_type="10-K"),
            ),
        )


@pytest.mark.parametrize(
    ("provider", "provider_issuer_id"),
    [("", "issuer-1"), ("sec", "")],
)
def test_issuer_reference_requires_nonempty_identity(
    provider: str,
    provider_issuer_id: str,
) -> None:
    with pytest.raises(ValueError):
        IssuerReference(provider=provider, provider_issuer_id=provider_issuer_id)


def test_filing_reference_derives_matching_issuer_identity() -> None:
    valid = filing_reference("filing-1")

    assert valid.issuer == IssuerReference(
        provider="sec",
        provider_issuer_id="issuer-1",
    )


def test_filing_reference_round_trips_its_workflow_contract() -> None:
    filing = filing_reference("filing-1")
    payload = filing.model_dump(mode="json")

    assert payload["document_policy"] == "primary"
    assert FilingReference.model_validate(payload) == filing


def test_filing_reference_contract_is_frozen_and_rejects_unknown_fields() -> None:
    filing = filing_reference("filing-1")
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
        ({"accepted_at": "2025-01-01T12:00:00"}, "accepted_at"),
    ],
)
def test_filing_reference_parser_rejects_invalid_temporal_fields(
    overrides: dict[str, object],
    message: str,
) -> None:
    payload: dict[str, object] = {
        **filing_reference("filing-1").model_dump(mode="json")
    }
    payload.update(overrides)

    with pytest.raises(ValueError, match=message):
        FilingReference.model_validate(payload)
