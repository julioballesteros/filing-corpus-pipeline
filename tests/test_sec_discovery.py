"""Tests for the shared SEC client and discovery-specific listing policy."""

from collections.abc import Callable
from datetime import UTC, date, datetime

import pytest

from filing_corpus_pipeline.discovery import (
    DiscoveryRequest,
    DiscoveryService,
    DiscoveryTarget,
)
from filing_corpus_pipeline.discovery.sec import SecFilingDiscoverySource
from filing_corpus_pipeline.domain import (
    DocumentPolicy,
    FilingSelection,
    IssuerReference,
)
from filing_corpus_pipeline.sources.http import HttpBytesResponse, HttpTransportError
from filing_corpus_pipeline.sources.sec import (
    SEC_DATA_BASE_URL,
    SecEdgarClient,
    SecEdgarClientConfig,
    SecRequestError,
    SecResponseError,
    normalize_cik,
)


class FakeTransport:
    """URL-addressed JSON transport with support for sequential outcomes."""

    def __init__(self, outcomes: dict[str, list[object]]) -> None:
        self.outcomes = outcomes
        self.calls: list[tuple[str, dict[str, str], float]] = []

    def get_json(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> object:
        self.calls.append((url, headers, timeout_seconds))
        outcome = self.outcomes[url].pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def get_bytes(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout_seconds: float,
        max_bytes: int,
    ) -> HttpBytesResponse:
        del url, headers, timeout_seconds, max_bytes
        raise AssertionError("discovery must not retrieve filing documents")


def columnar_payload(
    *,
    accessions: list[str],
    forms: list[str],
    filing_dates: list[str],
    report_dates: list[str] | None = None,
    acceptance_datetimes: list[str] | None = None,
    primary_documents: list[str] | None = None,
    items: list[str] | None = None,
) -> dict[str, object]:
    """Build the documented columnar shape returned by SEC submissions."""
    size = len(accessions)
    return {
        "accessionNumber": accessions,
        "form": forms,
        "filingDate": filing_dates,
        "reportDate": report_dates or [""] * size,
        "acceptanceDateTime": acceptance_datetimes or [""] * size,
        "primaryDocument": primary_documents or ["filing.htm"] * size,
        "items": items if items is not None else [""] * size,
    }


def main_payload(
    recent: dict[str, object],
    *,
    files: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    """Wrap recent rows in the top-level company response."""
    return {
        "name": "APPLE INC",
        "filings": {
            "recent": recent,
            "files": files or [],
        },
    }


def build_client(
    transport: FakeTransport,
    *,
    sleep: Callable[[float], None] = lambda _: None,
    metadata_max_attempts: int = 3,
    request_interval_seconds: float = 0,
) -> SecEdgarClient:
    """Build a client with deterministic timing."""
    return SecEdgarClient(
        transport=transport,
        config=SecEdgarClientConfig(
            user_agent="filing-corpus-pipeline contact@example.com",
            timeout_seconds=4.0,
            metadata_max_attempts=metadata_max_attempts,
            metadata_initial_backoff_seconds=0.25,
            request_interval_seconds=request_interval_seconds,
        ),
        sleep=sleep,
    )


def discovery_request(
    *,
    filing_types: tuple[str, ...] = ("10-K", "10-Q"),
    selections: tuple[FilingSelection, ...] | None = None,
    filed_from: date = date(2024, 1, 1),
    filed_to: date = date(2026, 8, 31),
) -> DiscoveryRequest:
    return DiscoveryRequest(
        targets=(
            DiscoveryTarget(
                company_id="apple-inc",
                issuer=IssuerReference(
                    provider="sec",
                    provider_issuer_id="320193",
                ),
                selections=(
                    selections
                    if selections is not None
                    else tuple(
                        FilingSelection(filing_type=filing_type)
                        for filing_type in filing_types
                    )
                ),
            ),
        ),
        filed_from=filed_from,
        filed_to=filed_to,
    )


def test_sec_source_returns_only_requested_forms_and_dates() -> None:
    """Recent and overlapping historical metadata map to stable references."""
    main_url = f"{SEC_DATA_BASE_URL}/CIK0000320193.json"
    history_name = "CIK0000320193-submissions-001.json"
    history_url = f"{SEC_DATA_BASE_URL}/{history_name}"
    recent = columnar_payload(
        accessions=[
            "0000320193-26-000003",
            "0000320193-26-000002",
            "0000320193-25-000001",
        ],
        forms=["10-Q", "8-K", "10-K"],
        filing_dates=["2026-08-01", "2026-06-01", "2025-02-01"],
        report_dates=["2026-06-30", "2026-05-31", "2024-12-31"],
        acceptance_datetimes=[
            "2026-08-01T10:30:00Z",
            "2026-06-01T10:30:00Z",
            "2025-02-01T10:30:00",
        ],
        primary_documents=["q3.htm", "current.htm", "annual.htm"],
    )
    historical = columnar_payload(
        accessions=["0000320193-24-000004", "0000320193-23-000005"],
        forms=["10-Q", "10-K"],
        filing_dates=["2024-08-01", "2023-02-01"],
        primary_documents=["q3-2024.htm", "annual-2022.htm"],
    )
    transport = FakeTransport(
        {
            main_url: [
                main_payload(
                    recent,
                    files=[
                        {
                            "name": history_name,
                            "filingFrom": "2022-01-01",
                            "filingTo": "2024-12-31",
                        }
                    ],
                )
            ],
            history_url: [historical],
        }
    )
    service = DiscoveryService([SecFilingDiscoverySource(build_client(transport))])
    request = discovery_request()

    result = service.execute(request)

    assert [filing.provider_filing_id for filing in result.filings] == [
        "0000320193-24-000004",
        "0000320193-25-000001",
        "0000320193-26-000003",
    ]
    latest = result.filings[-1]
    assert latest.company_id == "apple-inc"
    assert latest.issuer.provider_issuer_id == "0000320193"
    assert latest.issuer_name == "APPLE INC"
    assert latest.filing_type == "10-Q"
    assert latest.report_date == date(2026, 6, 30)
    assert latest.accepted_at == datetime(2026, 8, 1, 10, 30, tzinfo=UTC)
    assert latest.filing_detail_url.endswith(
        "/320193/000032019326000003/0000320193-26-000003-index.html"
    )
    assert latest.primary_document_url.endswith("/320193/000032019326000003/q3.htm")
    assert [call[0] for call in transport.calls] == [main_url, history_url]
    assert transport.calls[0][1]["User-Agent"].startswith("filing-corpus-pipeline")
    assert transport.calls[0][2] == 4.0


def test_discovery_skips_nonoverlapping_historical_files() -> None:
    """The feature listing policy does not retrieve irrelevant history chunks."""
    main_url = f"{SEC_DATA_BASE_URL}/CIK0000320193.json"
    transport = FakeTransport(
        {
            main_url: [
                main_payload(
                    columnar_payload(
                        accessions=["0000320193-26-000001"],
                        forms=["10-Q"],
                        filing_dates=["2026-08-01"],
                    ),
                    files=[
                        {
                            "name": "old.json",
                            "filingFrom": "2019-01-01",
                            "filingTo": "2020-12-31",
                        }
                    ],
                )
            ]
        }
    )

    service = DiscoveryService([SecFilingDiscoverySource(build_client(transport))])
    result = service.execute(
        discovery_request(
            filing_types=("10-Q",),
            filed_from=date(2026, 1, 1),
            filed_to=date(2026, 12, 31),
        )
    )

    assert len(result.filings) == 1
    assert [call[0] for call in transport.calls] == [main_url]


def test_sec_source_discovers_only_item_2_02_as_an_earnings_release() -> None:
    """An 8-K is not an earnings release merely because of its form type."""
    main_url = f"{SEC_DATA_BASE_URL}/CIK0000320193.json"
    transport = FakeTransport(
        {
            main_url: [
                main_payload(
                    columnar_payload(
                        accessions=[
                            "0000320193-26-000001",
                            "0000320193-26-000002",
                            "0000320193-26-000003",
                        ],
                        forms=["8-K", "8-K", "8-K/A"],
                        filing_dates=["2026-08-01"] * 3,
                        primary_documents=[
                            "earnings-8k.htm",
                            "other-8k.htm",
                            "amended-8k.htm",
                        ],
                        items=["2.02, 9.01", "1.01,9.01", "2.02,9.01"],
                    )
                )
            ]
        }
    )
    service = DiscoveryService([SecFilingDiscoverySource(build_client(transport))])

    result = service.execute(
        discovery_request(
            selections=(
                FilingSelection(
                    filing_type="8-K",
                    document_policy=DocumentPolicy.EARNINGS_RELEASE,
                ),
            )
        )
    )

    assert len(result.filings) == 1
    filing = result.filings[0]
    assert filing.provider_filing_id == "0000320193-26-000001"
    assert filing.filing_type == "8-K"
    assert filing.document_policy is DocumentPolicy.EARNINGS_RELEASE
    assert filing.primary_document == "earnings-8k.htm"
    assert filing.filing_detail_url.endswith(
        "/320193/000032019326000001/0000320193-26-000001-index.html"
    )


@pytest.mark.parametrize(
    "selection",
    [
        FilingSelection(filing_type="8-K"),
        FilingSelection(filing_type="S-1"),
    ],
)
def test_sec_source_rejects_an_unsupported_route_before_http(
    selection: FilingSelection,
) -> None:
    transport = FakeTransport({})
    service = DiscoveryService([SecFilingDiscoverySource(build_client(transport))])

    with pytest.raises(ValueError, match="unsupported SEC filing selection"):
        service.execute(discovery_request(selections=(selection,)))

    assert transport.calls == []


def test_client_retries_transient_transport_errors() -> None:
    """Retryable provider failures use bounded exponential backoff."""
    main_url = f"{SEC_DATA_BASE_URL}/CIK0000320193.json"
    transport = FakeTransport(
        {
            main_url: [
                HttpTransportError("rate limited", retryable=True, status_code=429),
                main_payload(
                    columnar_payload(accessions=[], forms=[], filing_dates=[])
                ),
            ]
        }
    )
    sleeps: list[float] = []

    result = build_client(transport, sleep=sleeps.append).get_company_submissions(
        "320193"
    )

    assert result.recent == ()
    assert sleeps == [0.25]
    assert len(transport.calls) == 2


def test_client_spaces_sequential_sec_requests() -> None:
    """One discovery execution stays below the configured SEC request rate."""
    main_url = f"{SEC_DATA_BASE_URL}/CIK0000320193.json"
    history_url = f"{SEC_DATA_BASE_URL}/history.json"
    transport = FakeTransport(
        {
            main_url: [
                main_payload(
                    columnar_payload(accessions=[], forms=[], filing_dates=[]),
                    files=[
                        {
                            "name": "history.json",
                            "filingFrom": "2025-01-01",
                            "filingTo": "2025-12-31",
                        }
                    ],
                )
            ],
            history_url: [columnar_payload(accessions=[], forms=[], filing_dates=[])],
        }
    )
    sleeps: list[float] = []

    client = build_client(
        transport,
        sleep=sleeps.append,
        request_interval_seconds=0.125,
    )
    company = client.get_company_submissions("320193")
    client.get_historical_submissions(
        company.files[0].name,
        cik=company.cik,
        issuer_name=company.issuer_name,
    )

    assert sleeps == [0.125]


@pytest.mark.parametrize(
    "descriptor",
    [
        {
            "name": "../history.json",
            "filingFrom": "2025-01-01",
            "filingTo": "2025-12-31",
        },
        {
            "name": "history.json",
            "filingFrom": "2025-12-31",
            "filingTo": "2025-01-01",
        },
    ],
)
def test_client_rejects_malformed_history_descriptors(
    descriptor: dict[str, str],
) -> None:
    """Invalid SEC history metadata cannot become a follow-up request."""
    main_url = f"{SEC_DATA_BASE_URL}/CIK0000320193.json"
    transport = FakeTransport(
        {
            main_url: [
                main_payload(
                    columnar_payload(accessions=[], forms=[], filing_dates=[]),
                    files=[descriptor],
                )
            ]
        }
    )

    with pytest.raises(SecResponseError):
        build_client(transport).get_company_submissions("320193")

    assert [call[0] for call in transport.calls] == [main_url]


@pytest.mark.parametrize(
    ("file_name", "issuer_name"),
    [("../history.json", "APPLE INC"), ("history.json", " ")],
)
def test_client_rejects_invalid_history_request_identity(
    file_name: str,
    issuer_name: str,
) -> None:
    """Callers cannot turn a history operation into an arbitrary URL."""
    transport = FakeTransport({})

    with pytest.raises(ValueError):
        build_client(transport).get_historical_submissions(
            file_name,
            cik="320193",
            issuer_name=issuer_name,
        )

    assert transport.calls == []


@pytest.mark.parametrize("retryable", [False, True])
def test_client_surfaces_terminal_request_errors(retryable: bool) -> None:
    """Permanent failures and exhausted retries become one provider exception."""
    main_url = f"{SEC_DATA_BASE_URL}/CIK0000320193.json"
    attempts = 2 if retryable else 1
    transport = FakeTransport(
        {
            main_url: [
                HttpTransportError("unavailable", retryable=retryable)
                for _ in range(attempts)
            ]
        }
    )

    with pytest.raises(SecRequestError, match="unavailable"):
        build_client(
            transport,
            metadata_max_attempts=attempts,
        ).get_company_submissions("320193")

    assert len(transport.calls) == attempts


@pytest.mark.parametrize(
    "value",
    ["", "abc", "12345678901", "\uff11\uff12\uff13"],
)
def test_normalize_cik_rejects_invalid_values(value: str) -> None:
    """Only one-to-ten ASCII digits are valid CIK input."""
    with pytest.raises(ValueError, match="invalid SEC CIK"):
        normalize_cik(value)


def test_normalize_cik_adds_leading_zeroes() -> None:
    """SEC data endpoints require their ten-digit CIK representation."""
    assert normalize_cik(" 320193 ") == "0000320193"


@pytest.mark.parametrize(
    "recent",
    [
        columnar_payload(
            accessions=["not-an-accession"],
            forms=["10-Q"],
            filing_dates=["2026-08-01"],
        ),
        columnar_payload(
            accessions=["0000320193-26-000001"],
            forms=["10-Q"],
            filing_dates=["bad-date"],
        ),
        columnar_payload(
            accessions=["0000320193-26-000001"],
            forms=["10-Q"],
            filing_dates=["2026-08-01"],
            primary_documents=[""],
        ),
        columnar_payload(
            accessions=["0000320193-26-000001"],
            forms=[],
            filing_dates=["2026-08-01"],
        ),
        columnar_payload(
            accessions=["0000320193-26-000001"],
            forms=["8-K"],
            filing_dates=["2026-08-01"],
            items=[],
        ),
    ],
)
def test_client_rejects_malformed_submission_rows(
    recent: dict[str, object],
) -> None:
    """Malformed provider data must not create ambiguous filing references."""
    main_url = f"{SEC_DATA_BASE_URL}/CIK0000320193.json"
    transport = FakeTransport({main_url: [main_payload(recent)]})

    with pytest.raises(SecResponseError):
        build_client(transport).get_company_submissions("320193")


@pytest.mark.parametrize(
    "config",
    [
        {"user_agent": ""},
        {"user_agent": "agent", "timeout_seconds": 0},
        {"user_agent": "agent", "metadata_max_attempts": 0},
        {"user_agent": "agent", "metadata_initial_backoff_seconds": -1},
        {"user_agent": "agent", "request_interval_seconds": -1},
    ],
)
def test_sec_client_config_rejects_invalid_policy(config: dict[str, object]) -> None:
    """Unsafe or nonsensical HTTP settings fail at composition time."""
    with pytest.raises(ValueError):
        SecEdgarClientConfig(**config)  # type: ignore[arg-type]
