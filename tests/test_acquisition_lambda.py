"""Tests for the dedicated raw-filing acquisition Lambda entrypoint."""

import logging
from datetime import UTC, date, datetime, timedelta

import pytest

from filing_corpus_pipeline.acquisition import (
    AcquisitionOutcome,
    AcquisitionRequest,
    AcquisitionResult,
    RetryableAcquisitionError,
)
from filing_corpus_pipeline.acquisition import handler as acquisition_handler
from filing_corpus_pipeline.acquisition.handler import InvalidAcquisitionEvent
from filing_corpus_pipeline.domain import FilingReference, SourceDocumentReference
from filing_corpus_pipeline.registry import RawDocumentMetadata
from filing_corpus_pipeline.runtime.config import LambdaConfigurationError


class CapturingAcquisitionService:
    """Capture one request or surface a scripted application failure."""

    def __init__(
        self,
        result: AcquisitionResult | Exception,
    ) -> None:
        self.result = result
        self.requests: list[AcquisitionRequest] = []

    def acquire(self, request: AcquisitionRequest) -> AcquisitionResult:
        self.requests.append(request)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def filing_reference() -> FilingReference:
    """Build a complete workflow filing record."""
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
        filing_detail_url="https://www.sec.gov/index.htm",
        primary_document_url="https://www.sec.gov/report.htm",
    )


def valid_event() -> dict[str, object]:
    """Build the Map-to-Lambda event contract."""
    return {
        "filing": filing_reference().model_dump(mode="json"),
        "owner_id": ("arn:aws:states:eu-west-1:123456789012:execution:workflow:run-1"),
        "requested_at": "2025-08-01T18:00:00Z",
    }


def configure_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the acquisition Lambda runtime contract."""
    monkeypatch.setenv("SEC_USER_AGENT", "pipeline contact@example.com")
    monkeypatch.setenv("REGISTRY_TABLE_NAME", "filing-registry")
    monkeypatch.setenv("RAW_BUCKET_NAME", "filing-corpus-raw")
    monkeypatch.setenv("ACQUISITION_LEASE_SECONDS", "300")
    monkeypatch.setenv("MAX_DOCUMENT_BYTES", str(25 * 1024 * 1024))
    monkeypatch.setenv("MAX_FILING_DETAIL_BYTES", str(2 * 1024 * 1024))


def stored_result() -> AcquisitionResult:
    """Build the bounded result returned to Step Functions."""
    return AcquisitionResult(
        filing_key="sec#0000320193-25-000079",
        outcome=AcquisitionOutcome.RAW_STORED,
        attempt_count=1,
        document=RawDocumentMetadata(
            source_document=SourceDocumentReference(
                document_name="aapl-20250628.htm",
                provider_document_type="10-Q",
                description=None,
                source_url="https://www.sec.gov/report.htm",
                resolver_version="sec-primary-v1",
            ),
            bucket="filing-corpus-raw",
            key="raw/sec/filing.htm",
            sha256="a" * 64,
            content_length=1024,
            content_type="text/html",
            version_id="version-1",
            etag="etag-1",
        ),
    )


def test_handler_composes_acquisition_and_returns_only_metadata(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One Map item becomes an application request without carrying bytes out."""
    configure_environment(monkeypatch)
    service = CapturingAcquisitionService(stored_result())
    composition_calls: list[dict[str, object]] = []

    def build_service(**kwargs: object) -> CapturingAcquisitionService:
        composition_calls.append(kwargs)
        return service

    monkeypatch.setattr(
        acquisition_handler,
        "build_acquisition_service",
        build_service,
    )
    caplog.set_level(logging.INFO, logger=acquisition_handler.__name__)

    result = acquisition_handler.handler(valid_event(), object())

    assert result == stored_result().model_dump(mode="json")
    assert "body" not in str(result)
    assert composition_calls == [
        {
            "registry_table_name": "filing-registry",
            "raw_bucket_name": "filing-corpus-raw",
            "max_document_bytes": 25 * 1024 * 1024,
            "max_filing_detail_bytes": 2 * 1024 * 1024,
        }
    ]
    request = service.requests[0]
    assert request.filing == filing_reference()
    assert request.requested_at == datetime(2025, 8, 1, 18, tzinfo=UTC)
    assert request.lease_duration == timedelta(minutes=5)
    completion = next(
        record
        for record in caplog.records
        if record.message == "Filing acquisition completed"
    )
    assert completion.__dict__["document_policy"] == "primary"
    assert completion.__dict__["source_document_name"] == "aapl-20250628.htm"
    assert completion.__dict__["source_document_type"] == "10-Q"
    assert completion.__dict__["resolver_version"] == "sec-primary-v1"


def test_handler_preserves_retryable_error_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Step Functions can match and retry the feature's explicit exception type."""
    configure_environment(monkeypatch)
    failure = RetryableAcquisitionError(
        "SEC unavailable",
        code="SEC_DOCUMENT_HTTP_ERROR",
        retryable=True,
    )
    service = CapturingAcquisitionService(failure)
    monkeypatch.setattr(
        acquisition_handler,
        "build_acquisition_service",
        lambda **_: service,
    )

    with pytest.raises(RetryableAcquisitionError) as raised:
        acquisition_handler.handler(valid_event(), object())

    assert raised.value is failure


@pytest.mark.parametrize(
    ("event", "message"),
    [
        ([], "event must be a JSON object"),
        ({}, "missing required field: filing"),
        ({**valid_event(), "filing": []}, "valid dictionary"),
        (
            {
                **valid_event(),
                "filing": {
                    **filing_reference().model_dump(mode="json"),
                    "form": "8-K",
                },
            },
            "form",
        ),
        ({**valid_event(), "owner_id": ""}, "owner_id must be a non-empty string"),
        (
            {**valid_event(), "requested_at": "not-a-time"},
            "requested_at must be an ISO timestamp",
        ),
        (
            {**valid_event(), "requested_at": "2025-08-01T18:00:00"},
            "requested_at must include a timezone offset",
        ),
    ],
)
def test_parser_rejects_invalid_map_input(event: object, message: str) -> None:
    """Malformed workflow state fails before an AWS client is constructed."""
    with pytest.raises(InvalidAcquisitionEvent, match=message):
        acquisition_handler.parse_acquisition_event(
            event,
            lease_duration=timedelta(minutes=5),
        )


def test_parser_translates_invalid_lease_policy() -> None:
    """Invalid composition policy is expressed at the entrypoint boundary."""
    with pytest.raises(InvalidAcquisitionEvent, match="lease_duration"):
        acquisition_handler.parse_acquisition_event(
            valid_event(),
            lease_duration=timedelta(0),
        )


@pytest.mark.parametrize(
    "missing_name",
    [
        "REGISTRY_TABLE_NAME",
        "RAW_BUCKET_NAME",
        "ACQUISITION_LEASE_SECONDS",
        "MAX_DOCUMENT_BYTES",
        "MAX_FILING_DETAIL_BYTES",
    ],
)
def test_handler_requires_runtime_configuration(
    monkeypatch: pytest.MonkeyPatch,
    missing_name: str,
) -> None:
    """A partially configured Lambda fails before touching provider or storage."""
    configure_environment(monkeypatch)
    monkeypatch.delenv(missing_name)

    with pytest.raises(LambdaConfigurationError, match=missing_name):
        acquisition_handler.handler(valid_event(), object())


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("ACQUISITION_LEASE_SECONDS", "zero"),
        ("ACQUISITION_LEASE_SECONDS", "0"),
        ("ACQUISITION_LEASE_SECONDS", "86401"),
        ("MAX_DOCUMENT_BYTES", "0"),
        ("MAX_DOCUMENT_BYTES", str(50 * 1024 * 1024 + 1)),
        ("MAX_FILING_DETAIL_BYTES", "0"),
        ("MAX_FILING_DETAIL_BYTES", str(10 * 1024 * 1024 + 1)),
    ],
)
def test_handler_rejects_invalid_resource_bounds(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    """Environment values cannot disable acquisition resource limits."""
    configure_environment(monkeypatch)
    monkeypatch.setenv(name, value)

    with pytest.raises(LambdaConfigurationError, match=name):
        acquisition_handler.handler(valid_event(), object())
