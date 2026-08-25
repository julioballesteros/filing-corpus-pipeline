"""Provider-neutral filing identifiers passed between pipeline stages."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Self


class FilingForm(StrEnum):
    """Filing forms supported by the initial corpus pipeline."""

    TEN_K = "10-K"
    TEN_Q = "10-Q"


@dataclass(frozen=True, slots=True)
class IssuerReference:
    """A source provider's stable identifier for an issuer."""

    provider: str
    provider_issuer_id: str

    def __post_init__(self) -> None:
        if not self.provider.strip():
            raise ValueError("provider must not be empty")
        if not self.provider_issuer_id.strip():
            raise ValueError("provider_issuer_id must not be empty")


@dataclass(frozen=True, slots=True)
class FilingReference:
    """The metadata required to start processing one filing."""

    provider: str
    provider_filing_id: str
    issuer: IssuerReference
    issuer_name: str
    form: FilingForm
    filed_on: date
    report_date: date | None
    accepted_at: datetime | None
    primary_document: str
    filing_detail_url: str
    primary_document_url: str

    def __post_init__(self) -> None:
        if self.provider != self.issuer.provider:
            raise ValueError("filing and issuer providers must match")
        for field_name in (
            "provider",
            "provider_filing_id",
            "issuer_name",
            "primary_document",
            "filing_detail_url",
            "primary_document_url",
        ):
            if not getattr(self, field_name).strip():
                raise ValueError(f"{field_name} must not be empty")

    def to_dict(self) -> dict[str, str | None]:
        """Return a JSON-compatible representation for workflow hand-off."""
        return {
            "provider": self.provider,
            "provider_filing_id": self.provider_filing_id,
            "provider_issuer_id": self.issuer.provider_issuer_id,
            "issuer_name": self.issuer_name,
            "form": self.form.value,
            "filed_on": self.filed_on.isoformat(),
            "report_date": (
                self.report_date.isoformat() if self.report_date is not None else None
            ),
            "accepted_at": (
                self.accepted_at.isoformat() if self.accepted_at is not None else None
            ),
            "primary_document": self.primary_document,
            "filing_detail_url": self.filing_detail_url,
            "primary_document_url": self.primary_document_url,
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        """Parse the JSON-compatible contract emitted by discovery."""
        if not isinstance(value, dict) or not all(
            isinstance(key, str) for key in value
        ):
            raise ValueError("filing must be a JSON object")
        payload: Mapping[str, object] = value
        provider = _required_string(payload, "provider")
        try:
            form = FilingForm(_required_string(payload, "form"))
        except ValueError as error:
            raise ValueError("form must be 10-K or 10-Q") from error

        accepted_at = _optional_datetime(payload, "accepted_at")
        if accepted_at is not None and (
            accepted_at.tzinfo is None or accepted_at.utcoffset() is None
        ):
            raise ValueError("accepted_at must include a timezone offset")
        return cls(
            provider=provider,
            provider_filing_id=_required_string(payload, "provider_filing_id"),
            issuer=IssuerReference(
                provider=provider,
                provider_issuer_id=_required_string(
                    payload,
                    "provider_issuer_id",
                ),
            ),
            issuer_name=_required_string(payload, "issuer_name"),
            form=form,
            filed_on=_required_date(payload, "filed_on"),
            report_date=_optional_date(payload, "report_date"),
            accepted_at=accepted_at,
            primary_document=_required_string(payload, "primary_document"),
            filing_detail_url=_required_string(payload, "filing_detail_url"),
            primary_document_url=_required_string(payload, "primary_document_url"),
        )


def _required_string(payload: Mapping[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _required_date(payload: Mapping[str, object], field: str) -> date:
    value = _required_string(payload, field)
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{field} must be an ISO date") from error


def _optional_date(payload: Mapping[str, object], field: str) -> date | None:
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO date or null")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{field} must be an ISO date or null") from error


def _optional_datetime(payload: Mapping[str, object], field: str) -> datetime | None:
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO timestamp or null")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field} must be an ISO timestamp or null") from error
