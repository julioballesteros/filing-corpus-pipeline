"""Tests for the feature-local AWS Lambda discovery handler."""

from datetime import date

import pytest

from filing_corpus_pipeline.discovery import (
    DiscoveryCompany,
    DiscoveryRequest,
    DiscoveryResult,
    DiscoveryTargetReference,
    DiscoveryTargetSet,
    DiscoveryWindow,
    RegulatorRegistration,
)
from filing_corpus_pipeline.discovery import handler as discovery_handler
from filing_corpus_pipeline.discovery.handler import InvalidDiscoveryEvent
from filing_corpus_pipeline.domain import FilingForm
from filing_corpus_pipeline.runtime.config import LambdaConfigurationError


class CapturingService:
    """Capture application inputs without reaching the SEC endpoint."""

    def __init__(self) -> None:
        self.requests: tuple[DiscoveryRequest, ...] | None = None

    def execute_many(
        self,
        requests: tuple[DiscoveryRequest, ...],
    ) -> DiscoveryResult:
        self.requests = requests
        return DiscoveryResult(
            filings=(),
            issuers_scanned=sum(len(request.issuers) for request in requests),
        )


class StubTargetRepository:
    """Return one deployed target set and record its immutable reference."""

    def __init__(self, targets: DiscoveryTargetSet) -> None:
        self.targets = targets
        self.reference: DiscoveryTargetReference | None = None

    def load(self, reference: DiscoveryTargetReference) -> DiscoveryTargetSet:
        self.reference = reference
        return self.targets


def target_reference() -> DiscoveryTargetReference:
    return DiscoveryTargetReference(
        bucket="target-config",
        key="discovery-targets/dev.json",
        version_id="version-1",
        sha256="a" * 64,
    )


def target_set() -> DiscoveryTargetSet:
    return DiscoveryTargetSet(
        schema_version=1,
        target_set_id="portfolio-core",
        revision=3,
        companies=(
            DiscoveryCompany(
                company_id="apple-inc",
                display_name="Apple Inc.",
                registrations=(
                    RegulatorRegistration(
                        regulator="sec",
                        issuer_id="0000320193",
                        filing_types=("10-K", "10-Q"),
                    ),
                ),
            ),
            DiscoveryCompany(
                company_id="microsoft-corp",
                display_name="Microsoft Corporation",
                registrations=(
                    RegulatorRegistration(
                        regulator="sec",
                        issuer_id="0000789019",
                        filing_types=("10-Q",),
                    ),
                ),
            ),
        ),
    )


def valid_event() -> dict[str, object]:
    """Return the explicit parent-workflow payload."""
    return {
        "window": {
            "filed_from": "2025-01-01",
            "filed_to": "2025-12-31",
        },
        "target_config": target_reference().model_dump(mode="json"),
    }


def test_handler_loads_targets_and_executes_per_registration_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Lambda resolves deployed targets separately from its time window."""
    service = CapturingService()
    repository = StubTargetRepository(target_set())
    user_agents: list[str] = []

    def build_service(user_agent: str) -> CapturingService:
        user_agents.append(user_agent)
        return service

    monkeypatch.setattr(discovery_handler, "build_sec_discovery_service", build_service)
    monkeypatch.setattr(
        discovery_handler,
        "build_discovery_target_repository",
        lambda: repository,
    )
    monkeypatch.setenv("SEC_USER_AGENT", "pipeline contact@example.com")

    result = discovery_handler.handler(valid_event(), object())

    assert result == {
        "filings": [],
        "filings_found": 0,
        "issuers_scanned": 2,
        "target_set": {
            "target_set_id": "portfolio-core",
            "revision": 3,
            "schema_version": 1,
            "bucket": "target-config",
            "key": "discovery-targets/dev.json",
            "version_id": "version-1",
            "sha256": "a" * 64,
        },
    }
    assert user_agents == ["pipeline contact@example.com"]
    assert repository.reference == target_reference()
    assert service.requests is not None
    assert [request.issuers[0].provider_issuer_id for request in service.requests] == [
        "0000320193",
        "0000789019",
    ]
    assert service.requests[0].forms == frozenset(FilingForm)
    assert service.requests[1].forms == frozenset({FilingForm.TEN_Q})
    assert all(request.filed_from == date(2025, 1, 1) for request in service.requests)


def test_handler_requires_sec_user_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing runtime identity fails before making an external request."""
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)

    with pytest.raises(LambdaConfigurationError, match="SEC_USER_AGENT"):
        discovery_handler.handler(valid_event(), object())


def test_parser_derives_a_global_rolling_window_from_scheduled_time() -> None:
    """Scheduled runs produce one deterministic UTC window for every target."""
    event = valid_event()
    event["window"] = {
        "scheduled_at": "2025-03-01T00:30:00+01:00",
        "lookback_days": 7,
    }

    invocation = discovery_handler.parse_discovery_event(event)

    assert invocation.window.filed_to == date(2025, 2, 28)
    assert invocation.window.filed_from == date(2025, 2, 21)
    assert invocation.target_config == target_reference()


@pytest.mark.parametrize(
    ("event", "message"),
    [
        ([], "event must be a JSON object"),
        ({}, "missing required field: window"),
        ({"window": {}}, "missing required field: target_config"),
        ({**valid_event(), "window": []}, "window must be a JSON object"),
        (
            {
                **valid_event(),
                "window": {
                    "filed_from": "not-a-date",
                    "filed_to": "2025-01-01",
                },
            },
            "filed_from must be an ISO date",
        ),
        (
            {
                **valid_event(),
                "window": {
                    "filed_from": "2026-01-01",
                    "filed_to": "2025-01-01",
                },
            },
            "filed_from must be on or before filed_to",
        ),
        (
            {
                **valid_event(),
                "window": {
                    "filed_from": "2025-01-01",
                    "filed_to": "2025-01-02",
                    "scheduled_at": "2025-01-01T00:00:00Z",
                    "lookback_days": 7,
                },
            },
            "not both",
        ),
        (
            {
                **valid_event(),
                "window": {"scheduled_at": "2025-01-01T00:00:00Z"},
            },
            "scheduled_at and lookback_days must be provided together",
        ),
        (
            {
                **valid_event(),
                "window": {
                    "scheduled_at": "2025-01-01T00:00:00",
                    "lookback_days": 7,
                },
            },
            "scheduled_at must include a timezone offset",
        ),
        (
            {
                **valid_event(),
                "target_config": {
                    **target_reference().model_dump(mode="json"),
                    "sha256": "bad",
                },
            },
            "target_config.sha256",
        ),
    ],
)
def test_parser_rejects_invalid_workflow_input(
    event: object,
    message: str,
) -> None:
    """Bad Step Functions input becomes a failed Lambda task."""
    with pytest.raises(InvalidDiscoveryEvent, match=message):
        discovery_handler.parse_discovery_event(event)


@pytest.mark.parametrize("lookback_days", [0, -1, 1.5, "7", True])
def test_parser_rejects_invalid_lookback_days(lookback_days: object) -> None:
    """A rolling window always uses a positive whole number of calendar days."""
    event = valid_event()
    event["window"] = {
        "scheduled_at": "2025-01-01T00:00:00Z",
        "lookback_days": lookback_days,
    }

    with pytest.raises(InvalidDiscoveryEvent, match="positive integer"):
        discovery_handler.parse_discovery_event(event)


def test_sec_request_translation_rejects_an_unsupported_regulator() -> None:
    """Additional regulator targets wait for the future source registry."""
    targets = DiscoveryTargetSet(
        schema_version=1,
        target_set_id="portfolio-core",
        revision=1,
        companies=(
            DiscoveryCompany(
                company_id="example-plc",
                display_name="Example plc",
                registrations=(
                    RegulatorRegistration(
                        regulator="fca",
                        issuer_id="213800EXAMPLE",
                        filing_types=("annual-report",),
                    ),
                ),
            ),
        ),
    )

    with pytest.raises(InvalidDiscoveryEvent, match=r"unsupported.*fca"):
        discovery_handler._sec_discovery_requests(
            targets,
            DiscoveryWindow(
                filed_from=date(2025, 1, 1),
                filed_to=date(2025, 1, 2),
            ),
        )


def test_sec_request_translation_rejects_an_unsupported_filing_type() -> None:
    """The target schema stays generic while this runtime remains 10-K/10-Q only."""
    targets = DiscoveryTargetSet(
        schema_version=1,
        target_set_id="portfolio-core",
        revision=1,
        companies=(
            DiscoveryCompany(
                company_id="apple-inc",
                display_name="Apple Inc.",
                registrations=(
                    RegulatorRegistration(
                        regulator="sec",
                        issuer_id="0000320193",
                        filing_types=("8-K",),
                    ),
                ),
            ),
        ),
    )

    with pytest.raises(InvalidDiscoveryEvent, match="unsupported SEC filing type"):
        discovery_handler._sec_discovery_requests(
            targets,
            DiscoveryWindow(
                filed_from=date(2025, 1, 1),
                filed_to=date(2025, 1, 2),
            ),
        )
