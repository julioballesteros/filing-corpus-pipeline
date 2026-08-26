"""S3 client for immutable, content-verified raw filing objects."""

import base64
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import NoReturn, Protocol

from pydantic import Field, model_validator

from filing_corpus_pipeline.models import (
    NonEmptyString,
    PipelineModel,
    Sha256Digest,
)
from filing_corpus_pipeline.registry.models import RawDocumentMetadata


class S3Api(Protocol):
    """Low-level S3 SDK operations used by the storage client."""

    def put_object(self, **kwargs: object) -> Mapping[str, object]:
        """Conditionally create one object."""

    def head_object(self, **kwargs: object) -> Mapping[str, object]:
        """Read object integrity and version metadata."""

    def get_object(self, **kwargs: object) -> Mapping[str, object]:
        """Read one bounded object body."""


class ReadableBody(Protocol):
    """Subset of botocore StreamingBody used by the storage client."""

    def read(self, amt: int | None = None) -> bytes:
        """Read at most ``amt`` bytes."""


class RawObjectStorageError(RuntimeError):
    """Expected S3 storage failure with workflow retry classification."""

    def __init__(self, message: str, *, code: str, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class RawObjectCollisionError(RawObjectStorageError):
    """A deterministic key already contains different source bytes."""


class RawObjectIntegrityError(RawObjectStorageError):
    """A stored raw object no longer matches its registry metadata."""


class RawObjectWrite(PipelineModel):
    """The raw object body and provenance supplied by acquisition."""

    key: NonEmptyString
    body: bytes = Field(min_length=1)
    sha256: Sha256Digest
    content_type: NonEmptyString
    filing_key: NonEmptyString
    source_url: NonEmptyString
    source_etag: str | None = None
    source_last_modified: str | None = None


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

    def load(self, document: RawDocumentMetadata, *, max_bytes: int) -> bytes:
        """Load a bounded raw object and verify its registered identity."""
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        if document.bucket != self._bucket_name:
            raise RawObjectIntegrityError(
                "raw registry metadata references an unexpected bucket",
                code="RAW_BUCKET_MISMATCH",
                retryable=False,
            )
        if document.content_length > max_bytes:
            raise RawObjectIntegrityError(
                f"raw object exceeds the {max_bytes}-byte normalization limit",
                code="RAW_OBJECT_TOO_LARGE",
                retryable=False,
            )
        request: dict[str, object] = {
            "Bucket": self._bucket_name,
            "Key": document.key,
        }
        if document.version_id is not None:
            request["VersionId"] = document.version_id
        try:
            response = self._api.get_object(**request)
            response_length = response.get("ContentLength")
            if response_length != document.content_length:
                raise RawObjectIntegrityError(
                    "raw object length does not match registry metadata",
                    code="RAW_OBJECT_LENGTH_MISMATCH",
                    retryable=False,
                )
            stream = response.get("Body")
            if not hasattr(stream, "read"):
                raise RawObjectIntegrityError(
                    "S3 returned an unreadable raw object body",
                    code="RAW_OBJECT_BODY_INVALID",
                    retryable=True,
                )
            body = stream.read(max_bytes + 1)
        except RawObjectStorageError:
            raise
        except Exception as error:
            _raise_s3_error(error, operation="raw object read")

        if not isinstance(body, bytes):
            raise RawObjectIntegrityError(
                "S3 returned a non-bytes raw object body",
                code="RAW_OBJECT_BODY_INVALID",
                retryable=True,
            )
        if len(body) != document.content_length:
            raise RawObjectIntegrityError(
                "raw object body length does not match registry metadata",
                code="RAW_OBJECT_LENGTH_MISMATCH",
                retryable=False,
            )
        if sha256(body).hexdigest() != document.sha256.lower():
            raise RawObjectIntegrityError(
                "raw object SHA-256 does not match registry metadata",
                code="RAW_OBJECT_DIGEST_MISMATCH",
                retryable=False,
            )
        return body

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


class NormalizedObjectStorageError(RuntimeError):
    """Expected normalized corpus storage failure."""

    def __init__(self, message: str, *, code: str, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class NormalizedObjectCollisionError(NormalizedObjectStorageError):
    """A deterministic corpus key already contains different bytes."""


class NormalizedCorpusWrite(PipelineModel):
    """Self-contained normalized artifact bytes ready for immutable storage."""

    prefix: NonEmptyString
    manifest: bytes = Field(min_length=1)
    manifest_sha256: Sha256Digest
    blocks: bytes = Field(min_length=1)
    blocks_sha256: Sha256Digest
    filing_key: NonEmptyString
    parser_version: NonEmptyString
    source_sha256: Sha256Digest

    @model_validator(mode="after")
    def _validate_artifact_digests(self) -> "NormalizedCorpusWrite":
        if sha256(self.manifest).hexdigest() != self.manifest_sha256:
            raise ValueError("manifest_sha256 does not match manifest bytes")
        if sha256(self.blocks).hexdigest() != self.blocks_sha256:
            raise ValueError("blocks_sha256 does not match block bytes")
        return self


@dataclass(frozen=True, slots=True)
class StoredNormalizedCorpus:
    """Committed normalized artifact keys returned to the registry."""

    bucket: str
    prefix: str
    manifest_key: str
    blocks_key: str
    reused_manifest: bool
    reused_blocks: bool


class S3NormalizedCorpusClient:
    """Publish immutable blocks first and the manifest commit marker last."""

    def __init__(self, api: S3Api, *, bucket_name: str) -> None:
        if not bucket_name.strip():
            raise ValueError("bucket_name must not be empty")
        self._api = api
        self._bucket_name = bucket_name

    def store(self, request: NormalizedCorpusWrite) -> StoredNormalizedCorpus:
        """Create or verify a complete, deterministic normalized corpus."""
        blocks_key = f"{request.prefix}/blocks.jsonl.gz"
        manifest_key = f"{request.prefix}/manifest.json"
        common_metadata = {
            "filing-key": request.filing_key,
            "parser-version": request.parser_version,
            "source-sha256": request.source_sha256.lower(),
        }
        reused_blocks = self._store_immutable(
            key=blocks_key,
            body=request.blocks,
            digest=request.blocks_sha256,
            content_type="application/x-ndjson",
            content_encoding="gzip",
            metadata=common_metadata,
        )
        reused_manifest = self._store_immutable(
            key=manifest_key,
            body=request.manifest,
            digest=request.manifest_sha256,
            content_type="application/json",
            content_encoding=None,
            metadata=common_metadata,
        )
        return StoredNormalizedCorpus(
            bucket=self._bucket_name,
            prefix=request.prefix,
            manifest_key=manifest_key,
            blocks_key=blocks_key,
            reused_manifest=reused_manifest,
            reused_blocks=reused_blocks,
        )

    def _store_immutable(
        self,
        *,
        key: str,
        body: bytes,
        digest: str,
        content_type: str,
        content_encoding: str | None,
        metadata: dict[str, str],
    ) -> bool:
        put: dict[str, object] = {
            "Bucket": self._bucket_name,
            "Key": key,
            "Body": body,
            "ContentType": content_type,
            "ChecksumAlgorithm": "SHA256",
            "ChecksumSHA256": base64.b64encode(bytes.fromhex(digest)).decode("ascii"),
            "Metadata": {**metadata, "sha256": digest.lower()},
            "ServerSideEncryption": "AES256",
            "IfNoneMatch": "*",
        }
        if content_encoding is not None:
            put["ContentEncoding"] = content_encoding
        try:
            self._api.put_object(**put)
            return False
        except Exception as error:
            if not _is_precondition_failure(error):
                _raise_normalized_s3_error(error, operation="corpus object write")

        try:
            existing = self._api.head_object(Bucket=self._bucket_name, Key=key)
        except Exception as error:
            _raise_normalized_s3_error(
                error,
                operation="existing corpus object verification",
            )
        stored_metadata = existing.get("Metadata")
        stored_digest = (
            stored_metadata.get("sha256")
            if isinstance(stored_metadata, Mapping)
            else None
        )
        if stored_digest != digest.lower() or existing.get("ContentLength") != len(
            body
        ):
            raise NormalizedObjectCollisionError(
                f"normalized object key {key!r} already contains different bytes",
                code="NORMALIZED_OBJECT_COLLISION",
                retryable=False,
            )
        return True


def _raise_s3_error(error: Exception, *, operation: str) -> NoReturn:
    code, status_code = _aws_error_details(error)
    retryable = _is_retryable_aws_failure(code, status_code)
    safe_code = code.upper() if code is not None else "SDK_ERROR"
    raise RawObjectStorageError(
        f"S3 {operation} failed ({code or 'SDK error'})",
        code=f"S3_{safe_code}"[:100],
        retryable=retryable,
    ) from error


def _raise_normalized_s3_error(error: Exception, *, operation: str) -> NoReturn:
    code, status_code = _aws_error_details(error)
    retryable = _is_retryable_aws_failure(code, status_code)
    safe_code = code.upper() if code is not None else "SDK_ERROR"
    raise NormalizedObjectStorageError(
        f"S3 {operation} failed ({code or 'SDK error'})",
        code=f"S3_{safe_code}"[:100],
        retryable=retryable,
    ) from error


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
