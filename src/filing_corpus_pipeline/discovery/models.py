"""Input and output contracts for a discovery execution."""

from datetime import date

from pydantic import Field, computed_field, model_validator

from filing_corpus_pipeline.domain import FilingForm, FilingReference, IssuerReference
from filing_corpus_pipeline.models import PipelineModel


class DiscoveryRequest(PipelineModel):
    """The bounded issuer, form, and date range to inspect."""

    issuers: tuple[IssuerReference, ...] = Field(min_length=1)
    forms: frozenset[FilingForm] = Field(min_length=1)
    filed_from: date
    filed_to: date

    @model_validator(mode="after")
    def _validate_date_range(self) -> "DiscoveryRequest":
        if self.filed_from > self.filed_to:
            raise ValueError("filed_from must be on or before filed_to")
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
