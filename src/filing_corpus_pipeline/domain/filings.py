"""Provider-neutral filing identifiers passed between pipeline stages."""

from datetime import date, datetime
from enum import StrEnum

from pydantic import AwareDatetime, field_validator

from filing_corpus_pipeline.models import NonEmptyString, PipelineModel


class FilingForm(StrEnum):
    """Filing forms supported by the initial corpus pipeline."""

    TEN_K = "10-K"
    TEN_Q = "10-Q"


class IssuerReference(PipelineModel):
    """A source provider's stable identifier for an issuer."""

    provider: NonEmptyString
    provider_issuer_id: NonEmptyString


class FilingReference(PipelineModel):
    """The metadata required to start processing one filing."""

    provider: NonEmptyString
    provider_filing_id: NonEmptyString
    provider_issuer_id: NonEmptyString
    issuer_name: NonEmptyString
    form: FilingForm
    filed_on: date
    report_date: date | None
    accepted_at: AwareDatetime | None
    primary_document: NonEmptyString
    filing_detail_url: NonEmptyString
    primary_document_url: NonEmptyString

    @field_validator("filed_on", "report_date", mode="before")
    @classmethod
    def _require_date_contract(cls, value: object) -> object:
        if value is not None and not isinstance(value, (str, date)):
            raise ValueError("must be an ISO date or null")
        return value

    @field_validator("accepted_at", mode="before")
    @classmethod
    def _require_datetime_contract(cls, value: object) -> object:
        if value is not None and not isinstance(value, (str, datetime)):
            raise ValueError("must be an ISO timestamp or null")
        return value

    @property
    def issuer(self) -> IssuerReference:
        """Return the issuer identity used by provider-facing services."""
        return IssuerReference(
            provider=self.provider,
            provider_issuer_id=self.provider_issuer_id,
        )
