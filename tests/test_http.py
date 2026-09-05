"""Tests for the standard-library JSON HTTP transport."""

from email.message import Message
from unittest.mock import MagicMock
from urllib.error import HTTPError, URLError

import pytest

from filing_corpus_pipeline.sources import http
from filing_corpus_pipeline.sources.http import (
    HttpTransportError,
    UrllibBytesTransport,
    UrllibJsonTransport,
)


def test_transport_decodes_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """The runtime transport returns decoded JSON using response charset metadata."""
    response = MagicMock()
    response.__enter__.return_value.read.return_value = b'{"ok": true}'
    response.__enter__.return_value.headers.get_content_charset.return_value = None
    urlopen = MagicMock(return_value=response)
    monkeypatch.setattr(http, "urlopen", urlopen)

    result = UrllibJsonTransport().get_json(
        "https://example.test/data.json",
        headers={"User-Agent": "test"},
        timeout_seconds=3.0,
    )

    assert result == {"ok": True}
    assert urlopen.call_args.kwargs["timeout"] == 3.0


@pytest.mark.parametrize(
    ("error", "retryable", "status_code"),
    [
        (
            HTTPError(
                "https://example.test",
                429,
                "rate limited",
                Message(),
                None,
            ),
            True,
            429,
        ),
        (
            HTTPError(
                "https://example.test",
                404,
                "not found",
                Message(),
                None,
            ),
            False,
            404,
        ),
        (URLError("offline"), True, None),
    ],
)
def test_transport_classifies_request_errors(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    retryable: bool,
    status_code: int | None,
) -> None:
    """The SEC client receives enough information to apply its retry policy."""
    monkeypatch.setattr(http, "urlopen", MagicMock(side_effect=error))

    with pytest.raises(HttpTransportError) as raised:
        UrllibJsonTransport().get_json(
            "https://example.test/data.json",
            headers={},
            timeout_seconds=3.0,
        )

    assert raised.value.retryable is retryable
    assert raised.value.status_code == status_code


def test_transport_rejects_invalid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """A successful HTTP status with invalid JSON is a permanent data failure."""
    response = MagicMock()
    response.__enter__.return_value.read.return_value = b"not-json"
    response.__enter__.return_value.headers.get_content_charset.return_value = "utf-8"
    monkeypatch.setattr(http, "urlopen", MagicMock(return_value=response))

    with pytest.raises(HttpTransportError) as raised:
        UrllibJsonTransport().get_json(
            "https://example.test/data.json",
            headers={},
            timeout_seconds=3.0,
        )

    assert raised.value.retryable is False


def test_bytes_transport_returns_bounded_body_and_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Binary retrieval retains only the response metadata needed downstream."""
    headers = Message()
    headers["Content-Type"] = "text/html; charset=utf-8"
    headers["Content-Length"] = "4"
    headers["ETag"] = '"source-etag"'
    headers["Last-Modified"] = "Fri, 01 Aug 2025 18:00:00 GMT"
    response = MagicMock()
    response.__enter__.return_value.headers = headers
    response.__enter__.return_value.read.return_value = b"body"
    monkeypatch.setattr(http, "urlopen", MagicMock(return_value=response))

    result = UrllibBytesTransport().get_bytes(
        "https://example.test/report.htm",
        headers={},
        timeout_seconds=5,
        max_bytes=10,
    )

    assert result.body == b"body"
    assert result.content_type == "text/html"
    assert result.etag == '"source-etag"'
    assert result.last_modified == "Fri, 01 Aug 2025 18:00:00 GMT"
    response.__enter__.return_value.read.assert_called_once_with(11)


@pytest.mark.parametrize("declared_length", ["11", "not-a-number"])
def test_bytes_transport_enforces_declared_and_actual_size_limits(
    monkeypatch: pytest.MonkeyPatch,
    declared_length: str,
) -> None:
    """A missing or dishonest Content-Length cannot cause an unbounded read."""
    headers = Message()
    headers["Content-Type"] = "text/html"
    headers["Content-Length"] = declared_length
    response = MagicMock()
    response.__enter__.return_value.headers = headers
    response.__enter__.return_value.read.return_value = b"x" * 11
    monkeypatch.setattr(http, "urlopen", MagicMock(return_value=response))

    with pytest.raises(HttpTransportError) as raised:
        UrllibBytesTransport().get_bytes(
            "https://example.test/report.htm",
            headers={},
            timeout_seconds=5,
            max_bytes=10,
        )

    assert raised.value.code == "RESPONSE_TOO_LARGE"
    assert raised.value.retryable is False


def test_bytes_transport_rejects_an_invalid_limit() -> None:
    """A programming error cannot silently disable the resource bound."""
    with pytest.raises(ValueError, match="positive"):
        UrllibBytesTransport().get_bytes(
            "https://example.test/report.htm",
            headers={},
            timeout_seconds=5,
            max_bytes=0,
        )


def test_bytes_transport_classifies_network_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Binary source clients receive retry information on network errors."""
    monkeypatch.setattr(http, "urlopen", MagicMock(side_effect=TimeoutError("slow")))

    with pytest.raises(HttpTransportError) as raised:
        UrllibBytesTransport().get_bytes(
            "https://example.test/report.htm",
            headers={},
            timeout_seconds=5,
            max_bytes=10,
        )

    assert raised.value.retryable is True
