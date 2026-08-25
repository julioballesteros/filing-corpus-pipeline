"""Tests for the AWS Lambda discovery adapter."""

from datetime import date

import pytest

from filing_corpus_pipeline.discovery import DiscoveryRequest, DiscoveryResult
from filing_corpus_pipeline.domain import FilingForm
from filing_corpus_pipeline.entrypoints import discovery_lambda
from filing_corpus_pipeline.entrypoints.discovery_lambda import (
    InvalidDiscoveryEvent,
)
from filing_corpus_pipeline.entrypoints.errors import LambdaConfigurationError


class CapturingService:
    """Capture application input without reaching the SEC endpoint."""

    def __init__(self) -> None:
        self.request: DiscoveryRequest | None = None

    def execute(self, request: DiscoveryRequest) -> DiscoveryResult:
        self.request = request
        return DiscoveryResult(filings=(), issuers_scanned=len(request.issuers))


def valid_event() -> dict[str, object]:
    """Return the minimal explicit parent-workflow payload."""
    return {
        "provider": "sec",
        "issuer_ids": ["320193", "320193", "789019"],
        "filed_from": "2025-01-01",
        "filed_to": "2025-12-31",
    }


def test_handler_executes_discovery_and_returns_json_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Lambda adapter composes configuration around the discovery use case."""
    service = CapturingService()
    user_agents: list[str] = []

    def build_service(user_agent: str) -> CapturingService:
        user_agents.append(user_agent)
        return service

    monkeypatch.setattr(discovery_lambda, "build_sec_discovery_service", build_service)
    monkeypatch.setenv("SEC_USER_AGENT", "pipeline contact@example.com")

    result = discovery_lambda.handler(valid_event(), object())

    assert result == {
        "filings": [],
        "filings_found": 0,
        "issuers_scanned": 2,
    }
    assert user_agents == ["pipeline contact@example.com"]
    assert service.request is not None
    assert [issuer.provider_issuer_id for issuer in service.request.issuers] == [
        "320193",
        "789019",
    ]
    assert service.request.forms == frozenset(FilingForm)
    assert service.request.filed_from == date(2025, 1, 1)


def test_handler_requires_sec_user_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing runtime identity fails before making an external request."""
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)

    with pytest.raises(LambdaConfigurationError, match="SEC_USER_AGENT"):
        discovery_lambda.handler(valid_event(), object())


def test_parser_accepts_an_explicit_form_subset() -> None:
    """The parent workflow can restrict a run to one supported form."""
    event = valid_event()
    event["forms"] = ["10-Q"]

    request = discovery_lambda.parse_discovery_event(event)

    assert request.forms == frozenset({FilingForm.TEN_Q})


def test_parser_derives_a_rolling_window_from_scheduled_time() -> None:
    """Scheduled runs produce deterministic UTC filing-date bounds."""
    event = valid_event()
    del event["filed_from"]
    del event["filed_to"]
    event["scheduled_at"] = "2025-03-01T00:30:00+01:00"
    event["lookback_days"] = 7

    request = discovery_lambda.parse_discovery_event(event)

    assert request.filed_to == date(2025, 2, 28)
    assert request.filed_from == date(2025, 2, 21)


@pytest.mark.parametrize(
    ("event", "message"),
    [
        ([], "event must be a JSON object"),
        ({}, "provider must be a non-empty string"),
        (
            {
                **valid_event(),
                "provider": "other",
            },
            "unsupported provider",
        ),
        (
            {
                **valid_event(),
                "issuer_ids": "320193",
            },
            "issuer_ids must be an array",
        ),
        (
            {
                **valid_event(),
                "issuer_ids": [],
            },
            "issuer_ids must not be empty",
        ),
        (
            {
                **valid_event(),
                "forms": ["8-K"],
            },
            "unsupported filing form",
        ),
        (
            {
                **valid_event(),
                "forms": [],
            },
            "at least one filing form",
        ),
        (
            {
                **valid_event(),
                "filed_from": "not-a-date",
            },
            "filed_from must be an ISO date",
        ),
        (
            {
                **valid_event(),
                "filed_from": "2026-01-01",
                "filed_to": "2025-01-01",
            },
            "filed_from must be on or before filed_to",
        ),
        (
            {
                **valid_event(),
                "scheduled_at": "2025-01-01T00:00:00Z",
                "lookback_days": 7,
            },
            "not both",
        ),
        (
            {
                **valid_event(),
                "filed_to": None,
            },
            "filed_to must be a non-empty string",
        ),
        (
            {
                "provider": "sec",
                "issuer_ids": ["320193"],
                "scheduled_at": "2025-01-01T00:00:00Z",
            },
            "scheduled_at and lookback_days must be provided together",
        ),
        (
            {
                "provider": "sec",
                "issuer_ids": ["320193"],
                "scheduled_at": "2025-01-01T00:00:00",
                "lookback_days": 7,
            },
            "scheduled_at must include a timezone offset",
        ),
        (
            {
                "provider": "sec",
                "issuer_ids": ["320193"],
                "scheduled_at": "not-a-timestamp",
                "lookback_days": 7,
            },
            "scheduled_at must be an ISO timestamp",
        ),
    ],
)
def test_parser_rejects_invalid_workflow_input(
    event: object,
    message: str,
) -> None:
    """Bad Step Functions input becomes a failed Lambda task."""
    with pytest.raises(InvalidDiscoveryEvent, match=message):
        discovery_lambda.parse_discovery_event(event)


@pytest.mark.parametrize("lookback_days", [0, -1, 1.5, "7", True])
def test_parser_rejects_invalid_lookback_days(lookback_days: object) -> None:
    """A rolling window always uses a positive whole number of calendar days."""
    event: dict[str, object] = {
        "provider": "sec",
        "issuer_ids": ["320193"],
        "scheduled_at": "2025-01-01T00:00:00Z",
        "lookback_days": lookback_days,
    }

    with pytest.raises(InvalidDiscoveryEvent, match="positive integer"):
        discovery_lambda.parse_discovery_event(event)
