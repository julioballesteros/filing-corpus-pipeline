"""Small HTTP client boundary shared by external source clients."""

import json
from dataclasses import dataclass
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class HttpTransportError(RuntimeError):
    """An HTTP or response-decoding failure."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        status_code: int | None = None,
        code: str = "HTTP_REQUEST_FAILED",
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code
        self.code = code


@dataclass(frozen=True, slots=True)
class HttpBytesResponse:
    """Bounded response body and the provenance headers needed by callers."""

    body: bytes
    content_type: str
    etag: str | None
    last_modified: str | None


class JsonHttpTransport(Protocol):
    """Contract for retrieving a JSON document."""

    def get_json(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> object:
        """Retrieve and decode one JSON response."""


class BytesHttpTransport(Protocol):
    """Contract for retrieving a response body without unbounded reads."""

    def get_bytes(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout_seconds: float,
        max_bytes: int,
    ) -> HttpBytesResponse:
        """Retrieve at most ``max_bytes`` response bytes."""


class HttpTransport(JsonHttpTransport, BytesHttpTransport, Protocol):
    """Complete transport required by a source client with mixed endpoints."""


class UrllibJsonTransport:
    """Standard-library JSON transport suitable for AWS Lambda."""

    def get_json(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> object:
        """Retrieve JSON and classify transport failures."""
        request = Request(url=url, headers=headers, method="GET")
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                body = response.read()
                charset = response.headers.get_content_charset() or "utf-8"
        except HTTPError as error:
            retryable = error.code == 429 or 500 <= error.code < 600
            raise HttpTransportError(
                f"GET {url} returned HTTP {error.code}",
                retryable=retryable,
                status_code=error.code,
            ) from error
        except (URLError, TimeoutError, OSError) as error:
            reason = getattr(error, "reason", error)
            raise HttpTransportError(
                f"GET {url} failed: {reason}",
                retryable=True,
            ) from error

        try:
            payload: object = json.loads(body.decode(charset))
        except (LookupError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise HttpTransportError(
                f"GET {url} returned invalid JSON",
                retryable=False,
            ) from error
        return payload


class UrllibBytesTransport:
    """Bounded standard-library byte transport for source documents."""

    def get_bytes(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout_seconds: float,
        max_bytes: int,
    ) -> HttpBytesResponse:
        """Retrieve bytes while classifying network and size failures."""
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")

        request = Request(url=url, headers=headers, method="GET")
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                declared_length = response.headers.get("Content-Length")
                if declared_length is not None:
                    try:
                        parsed_length = int(declared_length)
                    except ValueError:
                        parsed_length = None
                    if parsed_length is not None and parsed_length > max_bytes:
                        raise HttpTransportError(
                            f"GET {url} exceeds the {max_bytes}-byte response limit",
                            retryable=False,
                            code="RESPONSE_TOO_LARGE",
                        )

                body = response.read(max_bytes + 1)
                content_type = response.headers.get_content_type()
                etag = response.headers.get("ETag")
                last_modified = response.headers.get("Last-Modified")
        except HttpTransportError:
            raise
        except HTTPError as error:
            retryable = error.code == 429 or 500 <= error.code < 600
            raise HttpTransportError(
                f"GET {url} returned HTTP {error.code}",
                retryable=retryable,
                status_code=error.code,
            ) from error
        except (URLError, TimeoutError, OSError) as error:
            reason = getattr(error, "reason", error)
            raise HttpTransportError(
                f"GET {url} failed: {reason}",
                retryable=True,
            ) from error

        if len(body) > max_bytes:
            raise HttpTransportError(
                f"GET {url} exceeds the {max_bytes}-byte response limit",
                retryable=False,
                code="RESPONSE_TOO_LARGE",
            )
        return HttpBytesResponse(
            body=body,
            content_type=content_type,
            etag=etag,
            last_modified=last_modified,
        )


class UrllibHttpTransport(UrllibJsonTransport, UrllibBytesTransport):
    """Complete standard-library transport used by the SEC EDGAR client."""
