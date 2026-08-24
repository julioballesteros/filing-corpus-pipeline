"""Input and output contracts for a discovery execution."""

from dataclasses import dataclass
from datetime import date

from filing_corpus_pipeline.domain import FilingForm, FilingReference, IssuerReference


@dataclass(frozen=True, slots=True)
class DiscoveryRequest:
    """The bounded issuer, form, and date range to inspect."""

    issuers: tuple[IssuerReference, ...]
    forms: frozenset[FilingForm]
    filed_from: date
    filed_to: date

    def __post_init__(self) -> None:
        if not self.issuers:
            raise ValueError("at least one issuer is required")
        if not self.forms:
            raise ValueError("at least one filing form is required")
        if self.filed_from > self.filed_to:
            raise ValueError("filed_from must be on or before filed_to")


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    """Filing references produced by one discovery execution."""

    filings: tuple[FilingReference, ...]
    issuers_scanned: int

    def to_dict(self) -> dict[str, int | list[dict[str, str | None]]]:
        """Return the payload a future Lambda handler will return."""
        return {
            "issuers_scanned": self.issuers_scanned,
            "filings_found": len(self.filings),
            "filings": [filing.to_dict() for filing in self.filings],
        }
