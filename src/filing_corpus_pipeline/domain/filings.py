"""Provider-neutral filing identities and processing selections."""

from datetime import date, datetime
from enum import StrEnum

from pydantic import AwareDatetime, field_validator

from filing_corpus_pipeline.models import NonEmptyString, PipelineModel


class DocumentPolicy(StrEnum):
    """Stable document-selection intents whose support is stage-specific."""

    PRIMARY = "primary"
    EARNINGS_RELEASE = "earnings-release"


class FilingSelection(PipelineModel):
    """A source-qualified filing type and the document content to process."""

    filing_type: NonEmptyString
    document_policy: DocumentPolicy = DocumentPolicy.PRIMARY

    @field_validator("filing_type")
    @classmethod
    def _require_canonical_filing_type(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("must not contain surrounding space")
        return value


class IssuerReference(PipelineModel):
    """A source provider's stable identifier for an issuer."""

    provider: NonEmptyString
    provider_issuer_id: NonEmptyString


class FilingReference(PipelineModel):
    """The metadata required to start processing one filing."""

    company_id: NonEmptyString
    provider: NonEmptyString
    provider_filing_id: NonEmptyString
    provider_issuer_id: NonEmptyString
    issuer_name: NonEmptyString
    filing_type: NonEmptyString
    document_policy: DocumentPolicy = DocumentPolicy.PRIMARY
    filed_on: date
    report_date: date | None
    accepted_at: AwareDatetime | None
    primary_document: NonEmptyString
    filing_detail_url: NonEmptyString
    primary_document_url: NonEmptyString

    @field_validator("filing_type")
    @classmethod
    def _require_canonical_filing_type(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("must not contain surrounding space")
        return value

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
