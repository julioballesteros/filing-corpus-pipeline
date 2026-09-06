"""Tests for SEC primary-document acquisition."""

from datetime import date

import pytest

from filing_corpus_pipeline.acquisition import DocumentRetrievalError
from filing_corpus_pipeline.acquisition.sec import (
    SecDocumentConfig,
    SecFilingDocumentSource,
)
from filing_corpus_pipeline.domain import DocumentPolicy, FilingReference
from filing_corpus_pipeline.sources.http import HttpBytesResponse, HttpTransportError
from filing_corpus_pipeline.sources.sec import SecEdgarClient, SecEdgarClientConfig


class StubBytesTransport:
    """Record SEC requests and return scripted responses in order."""

    def __init__(self, results: tuple[HttpBytesResponse | Exception, ...]) -> None:
        self.results = list(results)
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
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def get_json(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> object:
        del url, headers, timeout_seconds
        raise AssertionError("acquisition must not retrieve submission metadata")


def filing_reference() -> FilingReference:
    """Build the canonical Apple SEC archive reference."""
    return FilingReference(
        company_id="apple-inc",
        provider="sec",
        provider_filing_id="0000320193-25-000079",
        provider_issuer_id="0000320193",
        issuer_name="Apple Inc.",
        filing_type="10-Q",
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
    *results: HttpBytesResponse | Exception,
) -> tuple[SecFilingDocumentSource, StubBytesTransport]:
    """Build the source with deterministic resource bounds."""
    transport = StubBytesTransport(results)
    return (
        SecFilingDocumentSource(
            client=SecEdgarClient(
                transport=transport,
                config=SecEdgarClientConfig(
                    user_agent="filing-corpus test@example.com",
                    timeout_seconds=4,
                    request_interval_seconds=0,
                ),
            ),
            config=SecDocumentConfig(
                max_document_bytes=100,
                max_filing_detail_bytes=1000,
            ),
        ),
        transport,
    )


def test_sec_source_retrieves_the_canonical_bounded_document() -> None:
    """Identity-derived URLs, access identity, and bounds reach the transport."""
    document_source, transport = source(
        HttpBytesResponse(
            b"<html>filing</html>",
            "text/html",
            '"source-etag"',
            "Fri, 01 Aug 2025 18:00:00 GMT",
        )
    )

    result = document_source.retrieve(filing_reference())

    assert result.body == b"<html>filing</html>"
    assert result.source_document.document_name == "aapl-20250628.htm"
    assert result.source_document.provider_document_type == "10-Q"
    assert result.source_document.source_url == filing_reference().primary_document_url
    assert result.source_document.resolver_version == "sec-primary-v1"
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
        (
            filing_reference().model_copy(
                update={"filing_detail_url": "https://evil.test/index.html"}
            ),
            "SEC_FILING_DETAIL_URL_MISMATCH",
        ),
    ],
)
def test_sec_source_rejects_inconsistent_identity_before_http(
    filing: FilingReference,
    code: str,
) -> None:
    """Workflow input cannot redirect the acquisition worker to another host."""
    document_source, transport = source(
        HttpBytesResponse(b"body", "text/html", None, None)
    )

    with pytest.raises(DocumentRetrievalError) as raised:
        document_source.retrieve(filing)

    assert raised.value.code == code
    assert raised.value.retryable is False
    assert transport.calls == []


def test_sec_source_rejects_a_different_provider() -> None:
    """The SEC source never consumes another provider's record."""
    other = filing_reference().model_copy(update={"provider": "other"})
    document_source, _ = source(HttpBytesResponse(b"body", "text/html", None, None))

    with pytest.raises(DocumentRetrievalError) as raised:
        document_source.retrieve(other)

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
    document_source, _ = source(transport_error)

    with pytest.raises(DocumentRetrievalError) as raised:
        document_source.retrieve(filing_reference())

    assert raised.value.code == code
    assert raised.value.retryable is retryable


def test_sec_source_rejects_empty_success() -> None:
    """A successful status with no bytes cannot become corpus provenance."""
    document_source, _ = source(HttpBytesResponse(b"", "text/html", None, None))

    with pytest.raises(DocumentRetrievalError) as raised:
        document_source.retrieve(filing_reference())

    assert raised.value.code == "SEC_EMPTY_DOCUMENT"


def earnings_filing() -> FilingReference:
    """Build an Item 2.02 filing already selected by discovery."""
    return filing_reference().model_copy(
        update={
            "filing_type": "8-K",
            "document_policy": DocumentPolicy.EARNINGS_RELEASE,
            "primary_document": "report.htm",
            "primary_document_url": (
                "https://www.sec.gov/Archives/edgar/data/320193/"
                "000032019325000079/report.htm"
            ),
        }
    )


def filing_detail(*rows: str) -> bytes:
    """Build the SEC document table consumed before exhibit retrieval."""
    return (
        '<table summary="Document Format Files">'
        "<tr><th>Seq</th><th>Description</th><th>Document</th>"
        "<th>Type</th><th>Size</th></tr>"
        f"{''.join(rows)}"
        "</table>"
    ).encode()


def filing_document_row(
    sequence: int,
    description: str,
    document_name: str,
    document_type: str,
    size: int,
) -> str:
    """Build one SEC filing-document row."""
    return (
        "<tr>"
        f"<td>{sequence}</td><td>{description}</td>"
        f'<td><a href="{document_name}">{document_name}</a></td>'
        f"<td>{document_type}</td><td>{size}</td>"
        "</tr>"
    )


def test_sec_source_resolves_and_retrieves_an_earnings_exhibit() -> None:
    """Earnings policy stores the selected exhibit rather than the 8-K cover."""
    index = filing_detail(
        filing_document_row(1, "8-K", "report.htm", "8-K", 90),
        filing_document_row(
            2,
            "Investor presentation",
            "slides.htm",
            "EX-99.1",
            95,
        ),
        filing_document_row(
            3,
            "Quarterly earnings release",
            "earnings.htm",
            "EX-99.2",
            80,
        ),
    )
    document_source, transport = source(
        HttpBytesResponse(index, "text/html", None, None),
        HttpBytesResponse(
            b"<html>earnings</html>",
            "text/html",
            '"earnings-etag"',
            None,
        ),
    )

    result = document_source.retrieve(earnings_filing())

    assert result.body == b"<html>earnings</html>"
    assert result.source_document.document_name == "earnings.htm"
    assert result.source_document.provider_document_type == "EX-99.2"
    assert result.source_document.description == "Quarterly earnings release"
    assert result.source_document.resolver_version == "sec-earnings-release-v1"
    assert result.source_etag == '"earnings-etag"'
    assert len(transport.calls) == 2
    assert transport.calls[0]["url"] == earnings_filing().filing_detail_url
    assert transport.calls[0]["max_bytes"] == 1000
    assert transport.calls[1]["url"] == (
        "https://www.sec.gov/Archives/edgar/data/320193/"
        "000032019325000079/earnings.htm"
    )
    assert transport.calls[1]["max_bytes"] == 100


@pytest.mark.parametrize(
    ("error", "code", "retryable"),
    [
        (
            HttpTransportError(
                "index too large",
                retryable=False,
                code="RESPONSE_TOO_LARGE",
            ),
            "SEC_FILING_DETAIL_TOO_LARGE",
            False,
        ),
        (
            HttpTransportError("SEC unavailable", retryable=True, status_code=503),
            "SEC_FILING_DETAIL_HTTP_ERROR",
            True,
        ),
    ],
)
def test_sec_source_translates_filing_detail_failures(
    error: HttpTransportError,
    code: str,
    retryable: bool,
) -> None:
    """Listing failures remain distinguishable from exhibit download failures."""
    document_source, _ = source(error)

    with pytest.raises(DocumentRetrievalError) as raised:
        document_source.retrieve(earnings_filing())

    assert raised.value.code == code
    assert raised.value.retryable is retryable


def test_sec_source_rejects_invalid_filing_detail_html() -> None:
    """Provider markup drift is a permanent, diagnosable source failure."""
    document_source, _ = source(
        HttpBytesResponse(b"<html>missing table</html>", "text/html", None, None)
    )

    with pytest.raises(DocumentRetrievalError) as raised:
        document_source.retrieve(earnings_filing())

    assert raised.value.code == "SEC_INVALID_FILING_DETAIL"
    assert raised.value.retryable is False


def test_sec_source_rejects_declared_oversized_exhibit_before_download() -> None:
    """SEC size metadata avoids a guaranteed-to-fail second request."""
    index = filing_detail(
        filing_document_row(
            2,
            "Quarterly earnings release",
            "earnings.htm",
            "EX-99.1",
            101,
        )
    )
    document_source, transport = source(
        HttpBytesResponse(index, "text/html", None, None)
    )

    with pytest.raises(DocumentRetrievalError) as raised:
        document_source.retrieve(earnings_filing())

    assert raised.value.code == "SEC_DOCUMENT_TOO_LARGE"
    assert len(transport.calls) == 1


def test_sec_source_requires_8_k_for_earnings_release_policy() -> None:
    """A cross-stage contract mismatch fails before filing-detail retrieval."""
    filing = filing_reference().model_copy(
        update={"document_policy": DocumentPolicy.EARNINGS_RELEASE}
    )
    document_source, transport = source(
        HttpBytesResponse(b"unused", "text/html", None, None)
    )

    with pytest.raises(DocumentRetrievalError) as raised:
        document_source.retrieve(filing)

    assert raised.value.code == "SEC_EARNINGS_RELEASE_REQUIRES_8_K"
    assert transport.calls == []


@pytest.mark.parametrize(
    "values",
    [
        {"max_document_bytes": 0},
        {"max_filing_detail_bytes": 0},
    ],
)
def test_sec_document_config_rejects_an_unbounded_size(
    values: dict[str, int],
) -> None:
    """Invalid runtime limits fail during composition."""
    with pytest.raises(ValueError):
        SecDocumentConfig(**values)
