"""Tests for SEC filing-document inventory retrieval and validation."""

from collections.abc import Mapping

import pytest

from filing_corpus_pipeline.sources.http import HttpBytesResponse, HttpTransportError
from filing_corpus_pipeline.sources.sec import (
    SecEdgarClient,
    SecEdgarClientConfig,
    SecRequestError,
    SecResponseError,
    sec_filing_detail_url,
    sec_filing_document_url,
)


class StubFilingDetailTransport:
    """Return one filing-detail response and record its bounded request."""

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

    def get_json(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> object:
        del url, headers, timeout_seconds
        raise AssertionError("filing inventory must not use submissions JSON")


def client(
    result: HttpBytesResponse | Exception,
) -> tuple[SecEdgarClient, StubFilingDetailTransport]:
    """Build a client with deterministic request settings."""
    transport = StubFilingDetailTransport(result)
    return (
        SecEdgarClient(
            transport=transport,
            config=SecEdgarClientConfig(
                user_agent="filing-corpus test@example.com",
                timeout_seconds=4,
                request_interval_seconds=0,
            ),
        ),
        transport,
    )


def response(body: str) -> HttpBytesResponse:
    """Wrap a filing-detail HTML fixture as a transport response."""
    return HttpBytesResponse(body.encode(), "text/html", None, None)


def document_row(
    *,
    sequence: str = "2",
    description: str = "EX-99.1 - QUARTERLY EARNINGS RELEASE",
    document_name: str = "earnings.htm",
    document_type: str = "EX-99.1",
    size: str = "45,000",
    linked: bool = True,
) -> str:
    """Build one SEC Document Format Files row."""
    document = (
        f'<a href="/Archives/example/{document_name}">{document_name}</a>'
        if linked
        else document_name
    )
    return (
        "<tr>"
        f"<td>{sequence}</td>"
        f"<td>{description}</td>"
        f"<td>{document}</td>"
        f"<td>{document_type}</td>"
        f"<td>{size}</td>"
        "</tr>"
    )


def filing_detail_html(*rows: str) -> str:
    """Wrap rows in the table identified by SEC filing-detail markup."""
    return (
        "<html><body>"
        '<table class="tableFile" summary="Document Format Files">'
        "<tr><th>Seq</th><th>Description</th><th>Document</th>"
        "<th>Type</th><th>Size</th></tr>"
        f"{''.join(rows)}"
        "</table>"
        '<table summary="Data Files">'
        f"{document_row(sequence='3', document_name='instance.xml', document_type='XML')}"
        "</table>"
        "</body></html>"
    )


def test_client_returns_typed_document_format_inventory() -> None:
    """Only source-document rows are parsed with canonical archive URLs."""
    body = filing_detail_html(
        document_row(
            sequence="1",
            description="8-K",
            document_name="report.htm",
            document_type="8-K",
            size="32,000",
        ).replace("</a>", "</a><span> iXBRL</span>"),
        document_row(),
        (
            "<tr><td>&nbsp;</td><td>Complete submission text file</td>"
            '<td><a href="submission.txt">submission.txt</a></td>'
            "<td>&nbsp;</td><td>120000</td></tr>"
        ),
    )
    sec_client, transport = client(response(body))

    documents = sec_client.get_filing_documents(
        cik="320193",
        accession_number="0000320193-26-000001",
        max_bytes=2_000_000,
    )

    assert len(documents) == 2
    assert documents[0].sequence == 1
    assert documents[0].document_name == "report.htm"
    assert documents[0].document_type == "8-K"
    assert documents[0].size_bytes == 32_000
    assert documents[1].description == "EX-99.1 - QUARTERLY EARNINGS RELEASE"
    assert documents[1].source_url == (
        "https://www.sec.gov/Archives/edgar/data/320193/"
        "000032019326000001/earnings.htm"
    )
    assert all(document.document_name != "instance.xml" for document in documents)
    assert transport.calls == [
        {
            "url": (
                "https://www.sec.gov/Archives/edgar/data/320193/"
                "000032019326000001/0000320193-26-000001-index.html"
            ),
            "headers": {
                "Accept": "text/html, application/xhtml+xml;q=0.9",
                "User-Agent": "filing-corpus test@example.com",
            },
            "timeout_seconds": 4,
            "max_bytes": 2_000_000,
        }
    ]


def test_client_normalizes_whitespace_and_nullable_description() -> None:
    """Presentation whitespace does not leak into resolver inputs."""
    body = filing_detail_html(
        document_row(
            description=" \n\t ",
            document_name="graphic.jpg",
            document_type=" GRAPHIC ",
            size="0",
        )
    )
    sec_client, _ = client(response(body))

    document = sec_client.get_filing_documents(
        cik="320193",
        accession_number="0000320193-26-000001",
        max_bytes=1024,
    )[0]

    assert document.description is None
    assert document.document_type == "GRAPHIC"
    assert document.size_bytes == 0


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (
            {"cik": "not-a-cik", "accession_number": "0000320193-26-000001"},
            "CIK",
        ),
        (
            {"cik": "320193", "accession_number": "not-an-accession"},
            "accession",
        ),
    ],
)
def test_client_rejects_invalid_filing_identity_before_http(
    arguments: Mapping[str, str],
    message: str,
) -> None:
    """Callers cannot turn filing inventory into an arbitrary URL request."""
    sec_client, transport = client(response(filing_detail_html(document_row())))

    with pytest.raises(ValueError, match=message):
        sec_client.get_filing_documents(
            **arguments,
            max_bytes=1024,
        )

    assert transport.calls == []


def test_client_rejects_an_unbounded_inventory_request() -> None:
    """The shared archive operation always imposes a response-size limit."""
    sec_client, transport = client(response(filing_detail_html(document_row())))

    with pytest.raises(ValueError, match="max_bytes"):
        sec_client.get_filing_documents(
            cik="320193",
            accession_number="0000320193-26-000001",
            max_bytes=0,
        )

    assert transport.calls == []


@pytest.mark.parametrize(
    ("error", "code", "retryable"),
    [
        (
            HttpTransportError(
                "too large",
                retryable=False,
                code="RESPONSE_TOO_LARGE",
            ),
            "RESPONSE_TOO_LARGE",
            False,
        ),
        (
            HttpTransportError(
                "unavailable",
                retryable=True,
                status_code=503,
            ),
            "HTTP_REQUEST_FAILED",
            True,
        ),
    ],
)
def test_client_preserves_inventory_transport_classification(
    error: HttpTransportError,
    code: str,
    retryable: bool,
) -> None:
    """The future resolver receives stable retry information from HTTP."""
    sec_client, _ = client(error)

    with pytest.raises(SecRequestError) as raised:
        sec_client.get_filing_documents(
            cik="320193",
            accession_number="0000320193-26-000001",
            max_bytes=1024,
        )

    assert raised.value.code == code
    assert raised.value.retryable is retryable


@pytest.mark.parametrize(
    "body",
    [
        "<html><body>no document table</body></html>",
        (
            filing_detail_html(document_row())
            + filing_detail_html(document_row(sequence="3"))
        ),
        filing_detail_html(),
        ('<table summary="Document Format Files">' f"{document_row()}"),
        filing_detail_html("<tr><td>1</td><td>8-K</td></tr>"),
        filing_detail_html(document_row(sequence="0")),
        filing_detail_html(document_row(sequence="one")),
        filing_detail_html(document_row(linked=False)),
        filing_detail_html(document_row(document_name="x" * 513)),
        filing_detail_html(document_row(document_name="../earnings.htm")),
        filing_detail_html(document_row(document_type="")),
        filing_detail_html(document_row(size="unknown")),
        filing_detail_html(
            document_row(),
            document_row(sequence="2", document_name="other.htm"),
        ),
        filing_detail_html(
            document_row(),
            document_row(sequence="3"),
        ),
        filing_detail_html(document_row(description="x" * 1001)),
        filing_detail_html(document_row(document_type="x" * 101)),
    ],
)
def test_client_rejects_ambiguous_or_malformed_document_inventory(body: str) -> None:
    """Provider response drift fails closed before document selection."""
    sec_client, _ = client(response(body))

    with pytest.raises(SecResponseError):
        sec_client.get_filing_documents(
            cik="320193",
            accession_number="0000320193-26-000001",
            max_bytes=2_000_000,
        )


def test_filing_url_builders_share_canonical_archive_identity() -> None:
    """Detail and document requests derive from validated filing identity."""
    assert sec_filing_detail_url("320193", "0000320193-26-000001") == (
        "https://www.sec.gov/Archives/edgar/data/320193/"
        "000032019326000001/0000320193-26-000001-index.html"
    )
    assert sec_filing_document_url(
        "320193",
        "0000320193-26-000001",
        "earnings.htm",
    ) == (
        "https://www.sec.gov/Archives/edgar/data/320193/"
        "000032019326000001/earnings.htm"
    )
