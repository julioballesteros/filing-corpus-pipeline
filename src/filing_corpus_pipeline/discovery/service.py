"""Provider-independent orchestration of a discovery request."""

from collections.abc import Iterable
from typing import Protocol

from filing_corpus_pipeline.discovery.models import DiscoveryRequest, DiscoveryResult
from filing_corpus_pipeline.domain import FilingReference


class FilingDiscoverySource(Protocol):
    """Port implemented by each upstream filing provider."""

    provider: str

    def discover(self, request: DiscoveryRequest) -> list[FilingReference]:
        """Return filing references matching a bounded request."""


class ConflictingFilingReferenceError(RuntimeError):
    """Raised when a provider returns inconsistent data for one filing ID."""


class DiscoveryService:
    """Validate and normalize the result of a provider discovery operation."""

    def __init__(self, source: FilingDiscoverySource) -> None:
        self._source = source

    def execute(self, request: DiscoveryRequest) -> DiscoveryResult:
        """Discover, deduplicate, and deterministically order filings."""
        return self.execute_many((request,))

    def execute_many(self, requests: Iterable[DiscoveryRequest]) -> DiscoveryResult:
        """Discover targets with distinct form selections as one stable result."""
        issuers_scanned = 0
        unique: dict[tuple[str, str], FilingReference] = {}
        for request in requests:
            issuers_scanned += len(request.issuers)
            self._add_request_filings(request, unique)

        filings = tuple(
            sorted(
                unique.values(),
                key=lambda filing: (
                    filing.filed_on,
                    filing.provider_filing_id,
                ),
            )
        )
        return DiscoveryResult(
            filings=filings,
            issuers_scanned=issuers_scanned,
        )

    def _add_request_filings(
        self,
        request: DiscoveryRequest,
        unique: dict[tuple[str, str], FilingReference],
    ) -> None:
        """Validate one request and add its provider records to an aggregate."""
        mismatched_issuers = [
            issuer
            for issuer in request.issuers
            if issuer.provider != self._source.provider
        ]
        if mismatched_issuers:
            raise ValueError(
                f"source {self._source.provider!r} cannot discover issuers from "
                f"{mismatched_issuers[0].provider!r}"
            )

        for filing in self._source.discover(request):
            key = (filing.provider, filing.provider_filing_id)
            existing = unique.get(key)
            if existing is not None and existing != filing:
                raise ConflictingFilingReferenceError(
                    f"conflicting references returned for {key[0]}:{key[1]}"
                )
            unique[key] = filing
