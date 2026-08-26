"""Tests for SEC primary-document acquisition."""

from datetime import date

import pytest

from filing_corpus_pipeline.acquisition import DocumentRetrievalError
from filing_corpus_pipeline.adapters.http import HttpBytesResponse, HttpTransportError
from filing_corpus_pipeline.adapters.sec.documents import (
    SecDocumentConfig,
    SecFilingDocumentSource,
)
from filing_corpus_pipeline.domain import FilingForm, FilingReference


class StubBytesTransport:
    """Record one SEC request and return a scripted response."""

    def __init__(self, result: HttpBytesResponse | Exception) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    def get_bytes(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout_seconds: float,
        max_bytes: int,
    ) -> HttpBytesResponse:
        self.calls.append(
            {
                "url": url,
                "headers": headers,
                "timeout_seconds": timeout_seconds,
                "max_bytes": max_bytes,
            }
        )
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def filing_reference() -> FilingReference:
    """Build the canonical Apple SEC archive reference."""
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
        filing_detail_url=(
            "https://www.sec.gov/Archives/edgar/data/320193/"
            "000032019325000079/0000320193-25-000079-index.html"
        ),
        primary_document_url=(
            "https://www.sec.gov/Archives/edgar/data/320193/"
            "000032019325000079/aapl-20250628.htm"
        ),
    )


def source(
    result: HttpBytesResponse | Exception,
) -> tuple[SecFilingDocumentSource, StubBytesTransport]:
    """Build the source with deterministic resource bounds."""
    transport = StubBytesTransport(result)
    return (
        SecFilingDocumentSource(
            transport=transport,
            config=SecDocumentConfig(
                user_agent="filing-corpus test@example.com",
                timeout_seconds=4,
                max_document_bytes=100,
            ),
        ),
        transport,
    )


def test_sec_source_retrieves_the_canonical_bounded_document() -> None:
    """Identity-derived URLs, access identity, and bounds reach the transport."""
    adapter, transport = source(
        HttpBytesResponse(
            b"<html>filing</html>",
            "text/html",
            '"source-etag"',
            "Fri, 01 Aug 2025 18:00:00 GMT",
        )
    )

    result = adapter.retrieve(filing_reference())

    assert result.body == b"<html>filing</html>"
    assert result.source_etag == '"source-etag"'
    assert transport.calls[0]["url"] == filing_reference().primary_document_url
    assert transport.calls[0]["max_bytes"] == 100
    headers = transport.calls[0]["headers"]
    assert isinstance(headers, dict)
    assert headers["User-Agent"] == "filing-corpus test@example.com"


@pytest.mark.parametrize(
    ("filing", "code"),
    [
        (
            filing_reference().model_copy(
                update={"primary_document_url": "https://evil.test/x"}
            ),
            "SEC_DOCUMENT_URL_MISMATCH",
        ),
        (
            filing_reference().model_copy(update={"provider_filing_id": "invalid"}),
            "SEC_INVALID_ACCESSION",
        ),
        (
            filing_reference().model_copy(update={"primary_document": "../report.htm"}),
            "SEC_INVALID_DOCUMENT_NAME",
        ),
        (
            filing_reference().model_copy(
                update={"provider_issuer_id": "not-a-cik"},
            ),
            "SEC_INVALID_CIK",
        ),
    ],
)
def test_sec_source_rejects_inconsistent_identity_before_http(
    filing: FilingReference,
    code: str,
) -> None:
    """Workflow input cannot redirect the acquisition worker to another host."""
    adapter, transport = source(HttpBytesResponse(b"body", "text/html", None, None))

    with pytest.raises(DocumentRetrievalError) as raised:
        adapter.retrieve(filing)

    assert raised.value.code == code
    assert raised.value.retryable is False
    assert transport.calls == []


def test_sec_source_rejects_a_different_provider() -> None:
    """The SEC adapter never consumes another provider's record."""
    other = filing_reference().model_copy(update={"provider": "other"})
    adapter, _ = source(HttpBytesResponse(b"body", "text/html", None, None))

    with pytest.raises(DocumentRetrievalError) as raised:
        adapter.retrieve(other)

    assert raised.value.code == "SEC_PROVIDER_MISMATCH"


@pytest.mark.parametrize(
    ("transport_error", "code", "retryable"),
    [
        (
            HttpTransportError("too large", retryable=False, code="RESPONSE_TOO_LARGE"),
            "SEC_DOCUMENT_TOO_LARGE",
            False,
        ),
        (
            HttpTransportError("HTTP 503", retryable=True, status_code=503),
            "SEC_DOCUMENT_HTTP_ERROR",
            True,
        ),
    ],
)
def test_sec_source_translates_transport_failures(
    transport_error: HttpTransportError,
    code: str,
    retryable: bool,
) -> None:
    """Provider failures use stable registry codes and retain retry policy."""
    adapter, _ = source(transport_error)

    with pytest.raises(DocumentRetrievalError) as raised:
        adapter.retrieve(filing_reference())

    assert raised.value.code == code
    assert raised.value.retryable is retryable


def test_sec_source_rejects_empty_success() -> None:
    """A successful status with no bytes cannot become corpus provenance."""
    adapter, _ = source(HttpBytesResponse(b"", "text/html", None, None))

    with pytest.raises(DocumentRetrievalError) as raised:
        adapter.retrieve(filing_reference())

    assert raised.value.code == "SEC_EMPTY_DOCUMENT"


@pytest.mark.parametrize(
    "config",
    [
        {"user_agent": ""},
        {"user_agent": "ua", "timeout_seconds": 0},
        {"user_agent": "ua", "max_document_bytes": 0},
    ],
)
def test_sec_document_config_rejects_unbounded_values(
    config: dict[str, object],
) -> None:
    """Invalid runtime limits fail during composition."""
    with pytest.raises(ValueError):
        SecDocumentConfig(**config)  # type: ignore[arg-type]
