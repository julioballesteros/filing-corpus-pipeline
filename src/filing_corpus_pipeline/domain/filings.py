"""Provider-neutral filing identifiers passed between pipeline stages."""

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum


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
