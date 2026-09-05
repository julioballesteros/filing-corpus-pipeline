"""AWS Lambda entry point for a filing discovery execution."""

import logging
import os
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta

from pydantic import ValidationError

from filing_corpus_pipeline.discovery import (
    DiscoveryInvocation,
    DiscoveryRequest,
    DiscoveryTargetReference,
    DiscoveryTargetSet,
    DiscoveryWindow,
    TargetedDiscoveryResult,
)
from filing_corpus_pipeline.discovery.composition import (
    build_discovery_target_repository,
    build_sec_discovery_service,
)
from filing_corpus_pipeline.discovery.targets import target_provenance
from filing_corpus_pipeline.domain import FilingForm, IssuerReference
from filing_corpus_pipeline.entrypoints.errors import LambdaConfigurationError
from filing_corpus_pipeline.models import validation_error_message

LOGGER = logging.getLogger(__name__)
SUPPORTED_REGULATOR = "sec"


class InvalidDiscoveryEvent(ValueError):
    """Raised when the Step Functions input violates the discovery contract."""


def handler(
    event: object,
    context: object,
) -> dict[str, object]:
    """Execute SEC discovery and return a JSON-compatible workflow payload."""
    del context
    invocation = parse_discovery_event(event)
    user_agent = os.environ.get("SEC_USER_AGENT")
    if not user_agent:
        raise LambdaConfigurationError("SEC_USER_AGENT must be configured")

    target_set = build_discovery_target_repository().load(invocation.target_config)
    requests = _sec_discovery_requests(target_set, invocation.window)
    result = build_sec_discovery_service(user_agent).execute_many(requests)
    targeted_result = TargetedDiscoveryResult(
        filings=result.filings,
        issuers_scanned=result.issuers_scanned,
        target_set=target_provenance(target_set, invocation.target_config),
    )
    LOGGER.info(
        "Filing discovery completed",
        extra={
            "regulator": SUPPORTED_REGULATOR,
            "target_set_id": target_set.target_set_id,
            "target_set_revision": target_set.revision,
            "target_config_version_id": invocation.target_config.version_id,
            "issuers_scanned": result.issuers_scanned,
            "filings_found": len(result.filings),
        },
    )
    return targeted_result.model_dump(mode="json")


def parse_discovery_event(event: object) -> DiscoveryInvocation:
    """Parse the stable input contract supplied by the parent workflow."""
    payload = _mapping(event, field="event")
    if "window" not in payload:
        raise InvalidDiscoveryEvent("missing required field: window")
    if "target_config" not in payload:
        raise InvalidDiscoveryEvent("missing required field: target_config")
    window_payload = _mapping(payload["window"], field="window")
    filed_from, filed_to = _filing_date_range(window_payload)
    try:
        target_config = DiscoveryTargetReference.model_validate(
            payload["target_config"]
        )
    except ValidationError as error:
        raise InvalidDiscoveryEvent(
            f"target_config.{validation_error_message(error)}"
        ) from error
    try:
        return DiscoveryInvocation(
            window=DiscoveryWindow(filed_from=filed_from, filed_to=filed_to),
            target_config=target_config,
        )
    except ValidationError as error:
        raise InvalidDiscoveryEvent(validation_error_message(error)) from error


def _sec_discovery_requests(
    target_set: DiscoveryTargetSet,
    window: DiscoveryWindow,
) -> tuple[DiscoveryRequest, ...]:
    """Translate per-registration target settings for the current SEC source."""
    registrations = [
        registration
        for company in target_set.companies
        for registration in company.registrations
    ]
    unsupported = sorted(
        {
            registration.regulator
            for registration in registrations
            if registration.regulator != SUPPORTED_REGULATOR
        }
    )
    if unsupported:
        raise InvalidDiscoveryEvent(
            "target configuration contains regulators unsupported by this runtime: "
            + ", ".join(unsupported)
        )

    requests: list[DiscoveryRequest] = []
    for registration in registrations:
        try:
            forms = frozenset(FilingForm(value) for value in registration.filing_types)
        except ValueError as error:
            raise InvalidDiscoveryEvent(
                f"unsupported SEC filing type for issuer {registration.issuer_id!r}: "
                f"{error}"
            ) from error
        requests.append(
            DiscoveryRequest(
                issuers=(
                    IssuerReference(
                        provider=SUPPORTED_REGULATOR,
                        provider_issuer_id=registration.issuer_id,
                    ),
                ),
                forms=forms,
                filed_from=window.filed_from,
                filed_to=window.filed_to,
            )
        )
    return tuple(requests)


def _mapping(value: object, *, field: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise InvalidDiscoveryEvent(f"{field} must be a JSON object")
    return value


def _required_string(payload: Mapping[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise InvalidDiscoveryEvent(f"{field} must be a non-empty string")
    return value


def _required_date(payload: Mapping[str, object], field: str) -> date:
    value = _required_string(payload, field)
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise InvalidDiscoveryEvent(
            f"{field} must be an ISO date in YYYY-MM-DD format"
        ) from error


def _filing_date_range(payload: Mapping[str, object]) -> tuple[date, date]:
    exact_fields = ("filed_from", "filed_to")
    rolling_fields = ("scheduled_at", "lookback_days")
    has_exact_field = any(field in payload for field in exact_fields)
    has_rolling_field = any(field in payload for field in rolling_fields)

    if has_exact_field and has_rolling_field:
        raise InvalidDiscoveryEvent(
            "use either filed_from/filed_to or scheduled_at/lookback_days, not both"
        )
    if has_exact_field:
        if not all(field in payload for field in exact_fields):
            raise InvalidDiscoveryEvent(
                "filed_from and filed_to must be provided together"
            )
        return (
            _required_date(payload, "filed_from"),
            _required_date(payload, "filed_to"),
        )
    if has_rolling_field:
        if not all(field in payload for field in rolling_fields):
            raise InvalidDiscoveryEvent(
                "scheduled_at and lookback_days must be provided together"
            )
        scheduled_at = _required_datetime(payload, "scheduled_at")
        lookback_days = payload["lookback_days"]
        if (
            isinstance(lookback_days, bool)
            or not isinstance(lookback_days, int)
            or lookback_days < 1
        ):
            raise InvalidDiscoveryEvent("lookback_days must be a positive integer")
        filed_to = scheduled_at.astimezone(UTC).date()
        return filed_to - timedelta(days=lookback_days), filed_to

    raise InvalidDiscoveryEvent(
        "provide filed_from/filed_to or scheduled_at/lookback_days"
    )


def _required_datetime(
    payload: Mapping[str, object],
    field: str,
) -> datetime:
    value = _required_string(payload, field)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise InvalidDiscoveryEvent(f"{field} must be an ISO timestamp") from error
    if parsed.tzinfo is None:
        raise InvalidDiscoveryEvent(f"{field} must include a timezone offset")
    return parsed
