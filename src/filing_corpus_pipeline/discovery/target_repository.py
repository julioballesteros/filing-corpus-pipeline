"""Load and validate the deployed discovery target manifest."""

import json
from hashlib import sha256

from pydantic import ValidationError

from filing_corpus_pipeline.discovery.targets import (
    DiscoveryTargetReference,
    DiscoveryTargetSet,
)
from filing_corpus_pipeline.models import validation_error_message
from filing_corpus_pipeline.storage.s3 import S3ObjectClient, S3ObjectReadError

MAX_TARGET_CONFIG_BYTES = 256 * 1024


class DiscoveryTargetLoadError(RuntimeError):
    """Base failure while retrieving or validating deployed targets."""


class RetryableDiscoveryTargetError(DiscoveryTargetLoadError):
    """Transient target-storage failure that Step Functions may retry."""


class InvalidDiscoveryTargetError(DiscoveryTargetLoadError):
    """Permanent target identity, integrity, or schema failure."""


class DiscoveryTargetRepository:
    """Load a target manifest by exact S3 version and verify its SHA-256."""

    def __init__(
        self,
        storage: S3ObjectClient,
        *,
        max_bytes: int = MAX_TARGET_CONFIG_BYTES,
    ) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self._storage = storage
        self._max_bytes = max_bytes

    def load(self, reference: DiscoveryTargetReference) -> DiscoveryTargetSet:
        """Return the strict manifest at the requested immutable identity."""
        try:
            body = self._storage.read(
                bucket=reference.bucket,
                key=reference.key,
                version_id=reference.version_id,
                max_bytes=self._max_bytes,
            )
        except S3ObjectReadError as error:
            error_type = (
                RetryableDiscoveryTargetError
                if error.retryable
                else InvalidDiscoveryTargetError
            )
            raise error_type(str(error)) from error

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
