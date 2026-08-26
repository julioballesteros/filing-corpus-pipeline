"""AWS Lambda entrypoint for normalizing one acquired filing."""

import logging
import os
from collections.abc import Mapping
from datetime import datetime, timedelta

from filing_corpus_pipeline.domain import FilingReference
from filing_corpus_pipeline.entrypoints.errors import LambdaConfigurationError
from filing_corpus_pipeline.normalization import (
    NormalizationError,
    NormalizationRequest,
)
from filing_corpus_pipeline.normalization.composition import (
    build_sec_normalization_service,
)

LOGGER = logging.getLogger(__name__)
MAX_LEASE_SECONDS = 24 * 60 * 60
MAX_DOCUMENT_BYTES = 50 * 1024 * 1024


class InvalidNormalizationEvent(ValueError):
    """Raised when a Map item violates the normalization event contract."""


def handler(event: object, context: object) -> dict[str, object]:
    """Normalize one raw filing and return its committed corpus identity."""
    del context
    registry_table_name = _required_environment("REGISTRY_TABLE_NAME")
    raw_bucket_name = _required_environment("RAW_BUCKET_NAME")
    normalized_bucket_name = _required_environment("NORMALIZED_BUCKET_NAME")
    lease_seconds = _positive_environment_integer(
        "NORMALIZATION_LEASE_SECONDS",
        maximum=MAX_LEASE_SECONDS,
    )
    max_document_bytes = _positive_environment_integer(
        "NORMALIZATION_MAX_DOCUMENT_BYTES",
        maximum=MAX_DOCUMENT_BYTES,
    )
    request = parse_normalization_event(
        event,
        lease_duration=timedelta(seconds=lease_seconds),
    )
    service = build_sec_normalization_service(
        registry_table_name=registry_table_name,
        raw_bucket_name=raw_bucket_name,
        normalized_bucket_name=normalized_bucket_name,
        max_document_bytes=max_document_bytes,
    )
    try:
        result = service.normalize(request)
    except NormalizationError as error:
        LOGGER.warning(
            "Filing normalization failed",
            extra={
                "provider": request.filing.provider,
                "provider_filing_id": request.filing.provider_filing_id,
                "failure_code": error.code,
                "retryable": error.retryable,
            },
        )
        raise

    LOGGER.info(
        "Filing normalization completed",
        extra={
            "provider": request.filing.provider,
            "provider_filing_id": request.filing.provider_filing_id,
            "filing_key": result.filing_key,
            "outcome": result.outcome.value,
            "attempt_count": result.attempt_count,
            "block_count": (
                result.corpus.block_count if result.corpus is not None else None
            ),
            "quality_status": (
                result.corpus.quality_status if result.corpus is not None else None
            ),
        },
    )
    return result.to_dict()


def parse_normalization_event(
    event: object,
    *,
    lease_duration: timedelta,
) -> NormalizationRequest:
    """Parse the stable event supplied by one Step Functions Map iteration."""
    payload = _mapping(event)
    if "filing" not in payload:
        raise InvalidNormalizationEvent("missing required field: filing")
    try:
        filing = FilingReference.from_dict(payload["filing"])
    except ValueError as error:
        raise InvalidNormalizationEvent(str(error)) from error
    try:
        return NormalizationRequest(
            filing=filing,
            owner_id=_required_string(payload, "owner_id"),
            requested_at=_required_datetime(payload, "requested_at"),
            lease_duration=lease_duration,
        )
    except ValueError as error:
        raise InvalidNormalizationEvent(str(error)) from error


def _mapping(event: object) -> Mapping[str, object]:
    if not isinstance(event, dict) or not all(isinstance(key, str) for key in event):
        raise InvalidNormalizationEvent("event must be a JSON object")
    return event


def _required_string(payload: Mapping[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise InvalidNormalizationEvent(f"{field} must be a non-empty string")
    return value


def _required_datetime(payload: Mapping[str, object], field: str) -> datetime:
    value = _required_string(payload, field)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise InvalidNormalizationEvent(f"{field} must be an ISO timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InvalidNormalizationEvent(f"{field} must include a timezone offset")
    return parsed


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise LambdaConfigurationError(f"{name} must be configured")
    return value


def _positive_environment_integer(name: str, *, maximum: int) -> int:
    value = _required_environment(name)
    try:
        parsed = int(value)
    except ValueError as error:
        raise LambdaConfigurationError(
            f"{name} must be a positive integer no greater than {maximum}"
        ) from error
    if parsed < 1 or parsed > maximum:
        raise LambdaConfigurationError(
            f"{name} must be a positive integer no greater than {maximum}"
        )
    return parsed
