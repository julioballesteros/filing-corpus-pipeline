"""AWS Lambda entry point for a filing discovery execution."""

import logging
import os
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta

from filing_corpus_pipeline.discovery import DiscoveryRequest
from filing_corpus_pipeline.discovery.composition import build_sec_discovery_service
from filing_corpus_pipeline.domain import FilingForm, IssuerReference
from filing_corpus_pipeline.entrypoints.errors import LambdaConfigurationError

LOGGER = logging.getLogger(__name__)
SUPPORTED_PROVIDER = "sec"


class InvalidDiscoveryEvent(ValueError):
    """Raised when the Step Functions input violates the discovery contract."""


def handler(
    event: object,
    context: object,
) -> dict[str, int | list[dict[str, str | None]]]:
    """Execute SEC discovery and return a JSON-compatible workflow payload."""
    del context
    request = parse_discovery_event(event)
    user_agent = os.environ.get("SEC_USER_AGENT")
    if not user_agent:
        raise LambdaConfigurationError("SEC_USER_AGENT must be configured")

    result = build_sec_discovery_service(user_agent).execute(request)
    LOGGER.info(
        "Filing discovery completed",
        extra={
            "provider": SUPPORTED_PROVIDER,
            "issuers_scanned": result.issuers_scanned,
            "filings_found": len(result.filings),
        },
    )
    return result.to_dict()


def parse_discovery_event(event: object) -> DiscoveryRequest:
    """Parse the stable input contract supplied by the parent workflow."""
    payload = _mapping(event, field="event")
    provider = _required_string(payload, "provider")
    if provider != SUPPORTED_PROVIDER:
        raise InvalidDiscoveryEvent(f"unsupported provider: {provider!r}")

    issuer_ids = _required_string_list(payload, "issuer_ids")
    unique_issuer_ids = tuple(dict.fromkeys(issuer_ids))
    if not unique_issuer_ids:
        raise InvalidDiscoveryEvent("issuer_ids must not be empty")

    form_values = payload.get("forms")
    if form_values is None:
        forms = frozenset(FilingForm)
    else:
        try:
            forms = frozenset(
                FilingForm(value) for value in _string_list(form_values, field="forms")
            )
        except ValueError as error:
            raise InvalidDiscoveryEvent(f"unsupported filing form: {error}") from error

    filed_from, filed_to = _filing_date_range(payload)
    try:
        return DiscoveryRequest(
            issuers=tuple(
                IssuerReference(
                    provider=SUPPORTED_PROVIDER,
                    provider_issuer_id=issuer_id,
                )
                for issuer_id in unique_issuer_ids
            ),
            forms=forms,
            filed_from=filed_from,
            filed_to=filed_to,
        )
    except ValueError as error:
        raise InvalidDiscoveryEvent(str(error)) from error


def _mapping(value: object, *, field: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise InvalidDiscoveryEvent(f"{field} must be a JSON object")
    return value


def _required_string(payload: Mapping[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise InvalidDiscoveryEvent(f"{field} must be a non-empty string")
    return value


def _required_string_list(
    payload: Mapping[str, object],
    field: str,
) -> list[str]:
    if field not in payload:
        raise InvalidDiscoveryEvent(f"missing required field: {field}")
    return _string_list(payload[field], field=field)


def _string_list(value: object, *, field: str) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise InvalidDiscoveryEvent(f"{field} must be an array of non-empty strings")
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
