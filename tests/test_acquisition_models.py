"""Tests for provider-neutral acquisition values."""

from datetime import UTC, date, datetime, timedelta

import pytest

from filing_corpus_pipeline.acquisition import (
    AcquisitionOutcome,
    AcquisitionRequest,
    AcquisitionResult,
    DocumentRetrievalError,
    RetrievedDocument,
    raw_document_key,
)
from filing_corpus_pipeline.domain import FilingReference
from filing_corpus_pipeline.registry import RawDocumentMetadata


def filing_reference() -> FilingReference:
    """Build a filing whose identity contains S3 path delimiters."""
    return FilingReference(
        company_id="issuer-one",
        provider="provider/one",
        provider_filing_id="filing #1",
        provider_issuer_id="issuer/1",
        issuer_name="Issuer",
        filing_type="annual-report",
        filed_on=date(2025, 1, 1),
        report_date=None,
        accepted_at=None,
        primary_document="report one.htm",
        filing_detail_url="https://example.test/index",
        primary_document_url="https://example.test/report",
    )


def document_metadata() -> RawDocumentMetadata:
    """Build metadata used by a successful result."""
    return RawDocumentMetadata(
        bucket="bucket",
        key="key",
        sha256="a" * 64,
        content_length=10,
        content_type="text/html",
    )


def test_raw_document_key_escapes_each_identity_segment() -> None:
    """Provider delimiters cannot alter the deterministic object hierarchy."""
    assert raw_document_key(filing_reference()) == (
        "raw/provider%2Fone/issuer%2F1/filing%20%231/report%20one.htm"
    )


def test_raw_document_key_rejects_s3_keys_over_the_limit() -> None:
    """Unexpectedly large provider IDs fail before an S3 call."""
    filing = filing_reference().model_copy(update={"primary_document": "x" * 1020})

    with pytest.raises(ValueError, match="key size"):
        raw_document_key(filing)


@pytest.mark.parametrize(
    "overrides",
    [
        {"owner_id": ""},
        {"requested_at": datetime(2025, 1, 1)},
        {"lease_duration": timedelta(0)},
        {"lease_duration": timedelta(days=2)},
    ],
)
def test_acquisition_request_rejects_invalid_claim_values(
    overrides: dict[str, object],
) -> None:
    """The feature rejects unsafe lease values before claiming work."""
    values: dict[str, object] = {
        "filing": filing_reference(),
        "owner_id": "execution-1",
        "requested_at": datetime(2025, 1, 1, tzinfo=UTC),
        "lease_duration": timedelta(minutes=5),
    }
    values.update(overrides)

    with pytest.raises(ValueError):
        AcquisitionRequest(**values)  # type: ignore[arg-type]


def test_retrieved_document_requires_response_metadata() -> None:
    """A provider result must identify its content type and source URL."""
    with pytest.raises(ValueError):
        RetrievedDocument(
            body=b"body",
            content_type="",
            source_url="https://example.test",
        )
    with pytest.raises(ValueError):
        RetrievedDocument(body=b"body", content_type="text/html", source_url="")


def test_acquisition_result_enforces_document_invariant() -> None:
    """Only RAW_STORED results may carry durable object metadata."""
    stored = AcquisitionResult(
        filing_key="sec#filing",
        outcome=AcquisitionOutcome.RAW_STORED,
        attempt_count=1,
        document=document_metadata(),
    )

    assert stored.document is not None
    assert stored.model_dump(mode="json") == {
        "filing_key": "sec#filing",
        "outcome": "RAW_STORED",
        "attempt_count": 1,
        "document": {
            "bucket": "bucket",
            "key": "key",
            "sha256": "a" * 64,
            "content_length": 10,
            "content_type": "text/html",
            "version_id": None,
            "etag": None,
        },
    }
    with pytest.raises(ValueError, match="required only"):
        AcquisitionResult(
            filing_key="sec#filing",
            outcome=AcquisitionOutcome.RAW_STORED,
            attempt_count=1,
        )
    with pytest.raises(ValueError, match="required only"):
        AcquisitionResult(
            filing_key="sec#filing",
            outcome=AcquisitionOutcome.ALREADY_COMPLETED,
            attempt_count=1,
            document=document_metadata(),
        )
    with pytest.raises(ValueError):
        AcquisitionResult(
            filing_key="",
            outcome=AcquisitionOutcome.ALREADY_COMPLETED,
            attempt_count=1,
        )
    with pytest.raises(ValueError):
        AcquisitionResult(
            filing_key="sec#filing",
            outcome=AcquisitionOutcome.ALREADY_COMPLETED,
            attempt_count=0,
        )

    duplicate = AcquisitionResult(
        filing_key="sec#filing",
        outcome=AcquisitionOutcome.ALREADY_COMPLETED,
        attempt_count=1,
    )
    assert duplicate.model_dump(mode="json")["document"] is None


@pytest.mark.parametrize(
    ("message", "code"),
    [("", "ERROR"), ("failed", ""), ("x" * 1001, "ERROR"), ("failed", "x" * 101)],
)
def test_retrieval_errors_are_bounded(message: str, code: str) -> None:
    """Provider diagnostics always fit the registry's bounded fields."""
    with pytest.raises(ValueError):
        DocumentRetrievalError(message, code=code, retryable=False)
