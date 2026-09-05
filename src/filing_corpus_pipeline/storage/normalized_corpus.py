"""S3 persistence for deterministic normalized corpus artifacts."""

import base64
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import NoReturn

from pydantic import Field, model_validator

from filing_corpus_pipeline.models import (
    NonEmptyString,
    PipelineModel,
    Sha256Digest,
)
from filing_corpus_pipeline.storage.s3 import (
    S3Api,
    classify_aws_error,
    is_precondition_failure,
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
            if not is_precondition_failure(error):
                _raise_s3_error(error, operation="corpus object write")

        try:
            existing = self._api.head_object(Bucket=self._bucket_name, Key=key)
        except Exception as error:
            _raise_s3_error(
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
    details = classify_aws_error(error)
    raise NormalizedObjectStorageError(
        f"S3 {operation} failed ({details.code or 'SDK error'})",
        code=details.storage_code,
        retryable=details.retryable,
    ) from error
