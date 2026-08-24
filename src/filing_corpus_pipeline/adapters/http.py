"""Small HTTP boundary that keeps provider clients deterministic in tests."""

import json
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
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


class JsonHttpTransport(Protocol):
    """Port for retrieving a JSON document."""

    def get_json(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> object:
        """Retrieve and decode one JSON response."""


class UrllibJsonTransport:
    """Standard-library implementation suitable for the local and Lambda runtime."""

    def get_json(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> object:
        """Retrieve JSON, classifying failures for the provider retry policy."""
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
        except URLError as error:
            raise HttpTransportError(
                f"GET {url} failed: {error.reason}",
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
