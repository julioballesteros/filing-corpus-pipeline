"""S3 persistence for immutable, content-verified raw filing documents."""

import base64
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import NoReturn

from pydantic import Field

from filing_corpus_pipeline.models import (
    NonEmptyString,
    PipelineModel,
    Sha256Digest,
)
from filing_corpus_pipeline.registry.models import RawDocumentMetadata
from filing_corpus_pipeline.storage.s3 import (
    S3Api,
    S3ObjectClient,
    S3ObjectReadError,
    classify_aws_error,
    is_precondition_failure,
    normalized_etag,
    optional_response_string,
)


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
        self._reader = S3ObjectClient(api)
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
            if is_precondition_failure(error):
                return self._reuse_existing(request)
            _raise_s3_error(error, operation="raw object write")

        return StoredRawObject(
            bucket=self._bucket_name,
            key=request.key,
            version_id=optional_response_string(response, "VersionId"),
            etag=normalized_etag(response.get("ETag")),
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
        try:
            body = self._reader.read(
                bucket=self._bucket_name,
                key=document.key,
                version_id=document.version_id,
                max_bytes=max_bytes,
            )
        except S3ObjectReadError as error:
            _raise_raw_read_error(error)
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
            version_id=optional_response_string(response, "VersionId"),
            etag=normalized_etag(response.get("ETag")),
            reused=True,
        )


def _raise_s3_error(error: Exception, *, operation: str) -> NoReturn:
    details = classify_aws_error(error)
    raise RawObjectStorageError(
        f"S3 {operation} failed ({details.code or 'SDK error'})",
        code=details.storage_code,
        retryable=details.retryable,
    ) from error


def _raise_raw_read_error(error: S3ObjectReadError) -> NoReturn:
    if error.code in {
        "S3_INVALID_CONTENT_LENGTH",
        "S3_LENGTH_MISMATCH",
        "S3_OBJECT_TOO_LARGE",
    }:
        raise RawObjectIntegrityError(
            "raw object length does not match registry metadata",
            code="RAW_OBJECT_LENGTH_MISMATCH",
            retryable=False,
        ) from error
    if error.code == "S3_INVALID_BODY":
        raise RawObjectIntegrityError(
            "S3 returned an unreadable raw object body",
            code="RAW_OBJECT_BODY_INVALID",
            retryable=True,
        ) from error
    raise RawObjectStorageError(
        str(error),
        code=error.code,
        retryable=error.retryable,
    ) from error
