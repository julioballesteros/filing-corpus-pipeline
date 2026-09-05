"""Low-level S3 operations shared by feature-specific repositories."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol


class S3ReadApi(Protocol):
    """Subset of boto3 required for bounded S3 reads."""

    def get_object(self, **kwargs: object) -> Mapping[str, object]:
        """Read one bounded object body."""


class S3Api(S3ReadApi, Protocol):
    """Additional boto3 S3 operations used by persistence clients."""

    def put_object(self, **kwargs: object) -> Mapping[str, object]:
        """Conditionally create one object."""

    def head_object(self, **kwargs: object) -> Mapping[str, object]:
        """Read object integrity and version metadata."""


@dataclass(frozen=True, slots=True)
class AwsErrorDetails:
    """Safe, SDK-independent AWS error classification."""

    code: str | None
    status_code: int | None
    retryable: bool

    @property
    def storage_code(self) -> str:
        """Return a bounded code suitable for persisted failure metadata."""
        safe_code = self.code.upper() if self.code is not None else "SDK_ERROR"
        return f"S3_{safe_code}"[:100]


class S3ObjectReadError(RuntimeError):
    """A classified failure while reading a bounded S3 object."""

    def __init__(self, message: str, *, code: str, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class S3ObjectClient:
    """Read versioned S3 objects with a mandatory in-memory size bound."""

    def __init__(self, api: S3ReadApi) -> None:
        self._api = api

    def read(
        self,
        *,
        bucket: str,
        key: str,
        max_bytes: int,
        version_id: str | None = None,
    ) -> bytes:
        """Return a complete object body after bounded length validation."""
        if not bucket.strip():
            raise ValueError("bucket must not be empty")
        if not key.strip():
            raise ValueError("key must not be empty")
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")

        request: dict[str, object] = {"Bucket": bucket, "Key": key}
        if version_id is not None:
            if not version_id.strip():
                raise ValueError("version_id must not be empty")
            request["VersionId"] = version_id
        try:
            response = self._api.get_object(**request)
        except Exception as error:
            details = classify_aws_error(error)
            raise S3ObjectReadError(
                f"S3 object read failed ({details.code or 'SDK error'})",
                code=details.storage_code,
                retryable=details.retryable,
            ) from error

        content_length = response.get("ContentLength")
        if not isinstance(content_length, int) or content_length < 0:
            raise S3ObjectReadError(
                "S3 object response has an invalid content length",
                code="S3_INVALID_CONTENT_LENGTH",
                retryable=False,
            )
        if content_length > max_bytes:
            raise S3ObjectReadError(
                f"S3 object exceeds the {max_bytes}-byte limit",
                code="S3_OBJECT_TOO_LARGE",
                retryable=False,
            )
        stream = response.get("Body")
        if not hasattr(stream, "read"):
            raise S3ObjectReadError(
                "S3 returned an unreadable object body",
                code="S3_INVALID_BODY",
                retryable=True,
            )
        try:
            body = stream.read(max_bytes + 1)
        except Exception as error:
            raise S3ObjectReadError(
                "S3 object body read failed",
                code="S3_BODY_READ_FAILED",
                retryable=True,
            ) from error
        if not isinstance(body, bytes):
            raise S3ObjectReadError(
                "S3 returned a non-bytes object body",
                code="S3_INVALID_BODY",
                retryable=True,
            )
        if len(body) > max_bytes:
            raise S3ObjectReadError(
                f"S3 object exceeds the {max_bytes}-byte limit",
                code="S3_OBJECT_TOO_LARGE",
                retryable=False,
            )
        if len(body) != content_length:
            raise S3ObjectReadError(
                "S3 object body length does not match its metadata",
                code="S3_LENGTH_MISMATCH",
                retryable=True,
            )
        return body


def classify_aws_error(error: Exception) -> AwsErrorDetails:
    """Extract botocore-style details without importing the AWS SDK."""
    response = getattr(error, "response", None)
    if not isinstance(response, Mapping):
        return AwsErrorDetails(code=None, status_code=None, retryable=True)

    error_value = response.get("Error")
    raw_code = error_value.get("Code") if isinstance(error_value, Mapping) else None
    metadata = response.get("ResponseMetadata")
    raw_status = (
        metadata.get("HTTPStatusCode") if isinstance(metadata, Mapping) else None
    )
    code = raw_code if isinstance(raw_code, str) else None
    status_code = raw_status if isinstance(raw_status, int) else None
    return AwsErrorDetails(
        code=code,
        status_code=status_code,
        retryable=_is_retryable_aws_failure(code, status_code),
    )


def is_precondition_failure(error: Exception) -> bool:
    """Return whether S3 rejected a create-only write because the key exists."""
    details = classify_aws_error(error)
    return details.code in {"PreconditionFailed", "412"} or details.status_code == 412


def optional_response_string(
    response: Mapping[str, object],
    field: str,
) -> str | None:
    """Read a non-empty optional string from an SDK response."""
    value = response.get(field)
    return value if isinstance(value, str) and value else None


def normalized_etag(value: object) -> str | None:
    """Normalize the optional quoted ETag returned by S3."""
    if not isinstance(value, str) or not value:
        return None
    return value[1:-1] if value.startswith('"') and value.endswith('"') else value


def _is_retryable_aws_failure(code: str | None, status_code: int | None) -> bool:
    return (
        status_code == 429
        or (status_code is not None and 500 <= status_code < 600)
        or code
        in {
            "ConditionalRequestConflict",
            "InternalError",
            "RequestTimeout",
            "ServiceUnavailable",
            "SlowDown",
            "Throttling",
            "ThrottlingException",
        }
        or status_code is None
    )
