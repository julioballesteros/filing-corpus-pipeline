"""Source-neutral input and output contracts for filing discovery."""

from datetime import date

from pydantic import Field, computed_field, model_validator

from filing_corpus_pipeline.domain import (
    FilingReference,
    FilingSelection,
    IssuerReference,
)
from filing_corpus_pipeline.models import NonEmptyString, PipelineModel


class DiscoveryTarget(PipelineModel):
    """One company registration and its requested filing selections."""

    company_id: NonEmptyString
    issuer: IssuerReference
    selections: tuple[FilingSelection, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_unique_filing_types(self) -> "DiscoveryTarget":
        filing_types = [selection.filing_type for selection in self.selections]
        if len(set(filing_types)) != len(filing_types):
            raise ValueError("selections must have unique filing_type values")
        return self


class DiscoveryRequest(PipelineModel):
    """The bounded source targets and date range to inspect."""

    targets: tuple[DiscoveryTarget, ...] = Field(min_length=1)
    filed_from: date
    filed_to: date

    @model_validator(mode="after")
    def _validate_date_range(self) -> "DiscoveryRequest":
        if self.filed_from > self.filed_to:
            raise ValueError("filed_from must be on or before filed_to")
        registrations = [
            (target.issuer.provider, target.issuer.provider_issuer_id)
            for target in self.targets
        ]
        if len(set(registrations)) != len(registrations):
            raise ValueError("targets must have unique provider/issuer identities")
        return self


class DiscoveryResult(PipelineModel):
    """Filing references produced by one discovery execution."""

    filings: tuple[FilingReference, ...]
    issuers_scanned: int = Field(ge=0)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def filings_found(self) -> int:
        """Return the number of filing records emitted by discovery."""
        return len(self.filings)
