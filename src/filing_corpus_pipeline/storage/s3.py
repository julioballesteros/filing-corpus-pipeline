"""S3 client for immutable, content-verified raw filing objects."""

import base64
from collections.abc import Mapping
from dataclasses import dataclass
from typing import NoReturn, Protocol


class S3Api(Protocol):
    """Low-level S3 SDK operations used by the storage client."""

    def put_object(self, **kwargs: object) -> Mapping[str, object]:
        """Conditionally create one object."""

    def head_object(self, **kwargs: object) -> Mapping[str, object]:
        """Read object integrity and version metadata."""


class RawObjectStorageError(RuntimeError):
    """Expected S3 storage failure with workflow retry classification."""

    def __init__(self, message: str, *, code: str, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class RawObjectCollisionError(RawObjectStorageError):
    """A deterministic key already contains different source bytes."""


@dataclass(frozen=True, slots=True)
class RawObjectWrite:
    """The raw object body and provenance supplied by acquisition."""

    key: str
    body: bytes
    sha256: str
    content_type: str
    filing_key: str
    source_url: str
    source_etag: str | None = None
    source_last_modified: str | None = None

    def __post_init__(self) -> None:
        for field in ("key", "content_type", "filing_key", "source_url"):
            if not str(getattr(self, field)).strip():
                raise ValueError(f"{field} must not be empty")
        if not self.body:
            raise ValueError("body must not be empty")
        if len(self.sha256) != 64:
            raise ValueError("sha256 must be a 64-character hexadecimal digest")
        try:
            bytes.fromhex(self.sha256)
        except ValueError as error:
            raise ValueError(
                "sha256 must be a 64-character hexadecimal digest"
            ) from error


@dataclass(frozen=True, slots=True)
class StoredRawObject:
    """Durable S3 identity returned without carrying the document body."""

    bucket: str
    key: str
    version_id: str | None
    etag: str | None
    reused: bool


class S3RawDocumentClient:
    """Create raw objects once and verify identical retries by SHA-256."""

    def __init__(self, api: S3Api, *, bucket_name: str) -> None:
        if not bucket_name.strip():
            raise ValueError("bucket_name must not be empty")
        self._api = api
        self._bucket_name = bucket_name

    def store(self, request: RawObjectWrite) -> StoredRawObject:
        """Atomically create a raw object or reuse an identical existing object."""
        metadata = {
            "sha256": request.sha256.lower(),
            "filing-key": request.filing_key,
            "source-url": request.source_url,
        }
        if request.source_etag is not None:
            metadata["source-etag"] = request.source_etag
        if request.source_last_modified is not None:
            metadata["source-last-modified"] = request.source_last_modified

        try:
            response = self._api.put_object(
                Bucket=self._bucket_name,
                Key=request.key,
                Body=request.body,
                ContentType=request.content_type,
                ChecksumAlgorithm="SHA256",
                ChecksumSHA256=base64.b64encode(bytes.fromhex(request.sha256)).decode(
                    "ascii"
                ),
                Metadata=metadata,
                ServerSideEncryption="AES256",
                IfNoneMatch="*",
            )
        except Exception as error:
            if _is_precondition_failure(error):
                return self._reuse_existing(request)
            _raise_s3_error(error, operation="raw object write")

        return StoredRawObject(
            bucket=self._bucket_name,
            key=request.key,
            version_id=_optional_response_string(response, "VersionId"),
            etag=_normalized_etag(response.get("ETag")),
            reused=False,
        )

    def _reuse_existing(self, request: RawObjectWrite) -> StoredRawObject:
        try:
            response = self._api.head_object(
                Bucket=self._bucket_name,
                Key=request.key,
            )
        except Exception as error:
            _raise_s3_error(error, operation="existing raw object verification")

        metadata = response.get("Metadata")
        existing_digest = (
            metadata.get("sha256") if isinstance(metadata, Mapping) else None
        )
        existing_length = response.get("ContentLength")
        if (
            not isinstance(existing_digest, str)
            or existing_digest.lower() != request.sha256.lower()
            or existing_length != len(request.body)
        ):
            raise RawObjectCollisionError(
                f"raw object key {request.key!r} already contains different bytes",
                code="RAW_OBJECT_COLLISION",
                retryable=False,
            )
        return StoredRawObject(
            bucket=self._bucket_name,
            key=request.key,
            version_id=_optional_response_string(response, "VersionId"),
            etag=_normalized_etag(response.get("ETag")),
            reused=True,
        )


def _raise_s3_error(error: Exception, *, operation: str) -> NoReturn:
    code, status_code = _aws_error_details(error)
    retryable = (
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
    safe_code = code.upper() if code is not None else "SDK_ERROR"
    raise RawObjectStorageError(
        f"S3 {operation} failed ({code or 'SDK error'})",
        code=f"S3_{safe_code}"[:100],
        retryable=retryable,
    ) from error


def _is_precondition_failure(error: Exception) -> bool:
    code, status_code = _aws_error_details(error)
    return code in {"PreconditionFailed", "412"} or status_code == 412


def _aws_error_details(error: Exception) -> tuple[str | None, int | None]:
    response = getattr(error, "response", None)
    if not isinstance(response, Mapping):
        return None, None

    error_value = response.get("Error")
    code = error_value.get("Code") if isinstance(error_value, Mapping) else None
    metadata = response.get("ResponseMetadata")
    status_code = (
        metadata.get("HTTPStatusCode") if isinstance(metadata, Mapping) else None
    )
    return (
        code if isinstance(code, str) else None,
        status_code if isinstance(status_code, int) else None,
    )


def _optional_response_string(response: Mapping[str, object], field: str) -> str | None:
    value = response.get(field)
    return value if isinstance(value, str) and value else None


def _normalized_etag(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value[1:-1] if value.startswith('"') and value.endswith('"') else value
