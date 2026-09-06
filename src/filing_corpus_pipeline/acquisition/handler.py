"""AWS Lambda handler for acquiring one discovered filing."""

import logging
from collections.abc import Mapping
from datetime import datetime, timedelta

from pydantic import ValidationError

from filing_corpus_pipeline.acquisition.composition import (
    build_acquisition_service,
)
from filing_corpus_pipeline.acquisition.models import AcquisitionRequest
from filing_corpus_pipeline.acquisition.service import AcquisitionError
from filing_corpus_pipeline.domain import FilingReference
from filing_corpus_pipeline.models import validation_error_message
from filing_corpus_pipeline.runtime.config import (
    positive_environment_integer,
    required_environment,
)

LOGGER = logging.getLogger(__name__)
MAX_LEASE_SECONDS = 24 * 60 * 60
MAX_DOCUMENT_BYTES = 50 * 1024 * 1024
MAX_FILING_DETAIL_BYTES = 10 * 1024 * 1024


class InvalidAcquisitionEvent(ValueError):
    """A Map item violates the acquisition event contract."""


def handler(event: object, context: object) -> dict[str, object]:
    """Acquire one filing and return only its disposition and S3 metadata."""
    del context
    registry_table_name = required_environment("REGISTRY_TABLE_NAME")
    raw_bucket_name = required_environment("RAW_BUCKET_NAME")
    lease_seconds = positive_environment_integer(
        "ACQUISITION_LEASE_SECONDS",
        maximum=MAX_LEASE_SECONDS,
    )
    max_document_bytes = positive_environment_integer(
        "MAX_DOCUMENT_BYTES",
        maximum=MAX_DOCUMENT_BYTES,
    )
    max_filing_detail_bytes = positive_environment_integer(
        "MAX_FILING_DETAIL_BYTES",
        maximum=MAX_FILING_DETAIL_BYTES,
    )
    request = parse_acquisition_event(
        event,
        lease_duration=timedelta(seconds=lease_seconds),
    )
    service = build_acquisition_service(
        registry_table_name=registry_table_name,
        raw_bucket_name=raw_bucket_name,
        max_document_bytes=max_document_bytes,
        max_filing_detail_bytes=max_filing_detail_bytes,
    )
    try:
        result = service.acquire(request)
    except AcquisitionError as error:
        LOGGER.warning(
            "Filing acquisition failed",
            extra={
                "provider": request.filing.provider,
                "provider_filing_id": request.filing.provider_filing_id,
                "filing_type": request.filing.filing_type,
                "document_policy": request.filing.document_policy.value,
                "failure_code": error.code,
                "retryable": error.retryable,
            },
        )
        raise

    LOGGER.info(
        "Filing acquisition completed",
        extra={
            "provider": request.filing.provider,
            "provider_filing_id": request.filing.provider_filing_id,
            "filing_type": request.filing.filing_type,
            "document_policy": request.filing.document_policy.value,
            "filing_key": result.filing_key,
            "outcome": result.outcome.value,
            "attempt_count": result.attempt_count,
            "source_document_name": (
                result.document.source_document.document_name
                if result.document is not None
                else None
            ),
            "source_document_type": (
                result.document.source_document.provider_document_type
                if result.document is not None
                else None
            ),
            "resolver_version": (
                result.document.source_document.resolver_version
                if result.document is not None
                else None
            ),
        },
    )
    return result.model_dump(mode="json")


def parse_acquisition_event(
    event: object,
    *,
    lease_duration: timedelta,
) -> AcquisitionRequest:
    """Parse the stable event supplied by one Step Functions Map iteration."""
    payload = _mapping(event)
    if "filing" not in payload:
        raise InvalidAcquisitionEvent("missing required field: filing")
    try:
        filing = FilingReference.model_validate(payload["filing"])
    except ValidationError as error:
        raise InvalidAcquisitionEvent(validation_error_message(error)) from error
    owner_id = _required_string(payload, "owner_id")
    requested_at = _required_datetime(payload, "requested_at")
    try:
        return AcquisitionRequest(
            filing=filing,
            owner_id=owner_id,
            requested_at=requested_at,
            lease_duration=lease_duration,
        )
    except ValidationError as error:
        raise InvalidAcquisitionEvent(validation_error_message(error)) from error


def _mapping(event: object) -> Mapping[str, object]:
    if not isinstance(event, dict) or not all(isinstance(key, str) for key in event):
        raise InvalidAcquisitionEvent("event must be a JSON object")
    return event


def _required_string(payload: Mapping[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise InvalidAcquisitionEvent(f"{field} must be a non-empty string")
    return value


def _required_datetime(payload: Mapping[str, object], field: str) -> datetime:
    value = _required_string(payload, field)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise InvalidAcquisitionEvent(f"{field} must be an ISO timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InvalidAcquisitionEvent(f"{field} must include a timezone offset")
    return parsed
