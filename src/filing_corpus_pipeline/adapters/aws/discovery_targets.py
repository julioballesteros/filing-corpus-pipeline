"""Load immutable, integrity-checked discovery targets from S3."""

import json
from collections.abc import Mapping
from hashlib import sha256
from typing import NoReturn, Protocol

from pydantic import ValidationError

from filing_corpus_pipeline.discovery.targets import (
    DiscoveryTargetReference,
    DiscoveryTargetSet,
)
from filing_corpus_pipeline.models import validation_error_message

MAX_TARGET_CONFIG_BYTES = 256 * 1024


class DiscoveryTargetS3Api(Protocol):
    """Low-level S3 operation required by the target repository."""

    def get_object(self, **kwargs: object) -> Mapping[str, object]:
        """Read one exact S3 object version."""


class ReadableBody(Protocol):
    """Subset of botocore StreamingBody used by the repository."""

    def read(self, amt: int | None = None) -> bytes:
        """Read at most ``amt`` bytes."""


class DiscoveryTargetLoadError(RuntimeError):
    """Base failure while retrieving or validating deployed targets."""


class RetryableDiscoveryTargetError(DiscoveryTargetLoadError):
    """Transient S3 failure that Step Functions may retry."""


class InvalidDiscoveryTargetError(DiscoveryTargetLoadError):
    """Permanent target identity, integrity, or schema failure."""


class S3DiscoveryTargetRepository:
    """Load a bounded target manifest by exact S3 version and SHA-256."""

    def __init__(
        self,
        api: DiscoveryTargetS3Api,
        *,
        max_bytes: int = MAX_TARGET_CONFIG_BYTES,
    ) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self._api = api
        self._max_bytes = max_bytes

    def load(self, reference: DiscoveryTargetReference) -> DiscoveryTargetSet:
        """Return the validated manifest at the requested immutable identity."""
        try:
            response = self._api.get_object(
                Bucket=reference.bucket,
                Key=reference.key,
                VersionId=reference.version_id,
            )
        except Exception as error:
            _raise_s3_load_error(error)

        content_length = response.get("ContentLength")
        if not isinstance(content_length, int) or content_length < 0:
            raise InvalidDiscoveryTargetError(
                "target configuration response has an invalid content length"
            )
        if content_length > self._max_bytes:
            raise InvalidDiscoveryTargetError(
                f"target configuration exceeds the {self._max_bytes}-byte limit"
            )

        stream = response.get("Body")
        if not hasattr(stream, "read"):
            raise RetryableDiscoveryTargetError(
                "S3 returned an unreadable target configuration body"
            )
        try:
            body = stream.read(self._max_bytes + 1)
        except Exception as error:
            raise RetryableDiscoveryTargetError(
                "target configuration body read failed"
            ) from error
        if not isinstance(body, bytes):
            raise RetryableDiscoveryTargetError(
                "S3 returned a non-bytes target configuration body"
            )
        if len(body) > self._max_bytes:
            raise InvalidDiscoveryTargetError(
                f"target configuration exceeds the {self._max_bytes}-byte limit"
            )
        if len(body) != content_length:
            raise RetryableDiscoveryTargetError(
                "target configuration body length does not match S3 metadata"
            )

        if sha256(body).hexdigest() != reference.sha256:
            raise InvalidDiscoveryTargetError(
                "target configuration SHA-256 does not match its deployed identity"
            )

        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise InvalidDiscoveryTargetError(
                "target configuration is not valid UTF-8 JSON"
            ) from error
        try:
            return DiscoveryTargetSet.model_validate(payload)
        except ValidationError as error:
            raise InvalidDiscoveryTargetError(
                f"target configuration is invalid: {validation_error_message(error)}"
            ) from error


def _raise_s3_load_error(error: Exception) -> NoReturn:
    response = getattr(error, "response", None)
    code: str | None = None
    status_code: int | None = None
    if isinstance(response, Mapping):
        error_payload = response.get("Error")
        if isinstance(error_payload, Mapping):
            value = error_payload.get("Code")
            code = value if isinstance(value, str) else None
        metadata = response.get("ResponseMetadata")
        if isinstance(metadata, Mapping):
            value = metadata.get("HTTPStatusCode")
            status_code = value if isinstance(value, int) else None

    retryable = (
        status_code == 429
        or (status_code is not None and status_code >= 500)
        or code
        in {
            "InternalError",
            "RequestTimeout",
            "ServiceUnavailable",
            "SlowDown",
            "Throttling",
            "ThrottlingException",
        }
        or (code is None and status_code is None)
    )
    message = "target configuration S3 read failed"
    if code is not None:
        message = f"{message}: {code}"
    error_type = (
        RetryableDiscoveryTargetError if retryable else InvalidDiscoveryTargetError
    )
    raise error_type(message) from error
