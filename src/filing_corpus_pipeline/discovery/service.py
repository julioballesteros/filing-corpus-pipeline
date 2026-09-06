"""Source-neutral orchestration of filing discovery."""

from collections import defaultdict
from collections.abc import Iterable
from datetime import date
from typing import Protocol

from filing_corpus_pipeline.discovery.models import (
    DiscoveryRequest,
    DiscoveryResult,
    DiscoveryTarget,
)
from filing_corpus_pipeline.domain import DocumentPolicy, FilingReference


class FilingDiscoverySource(Protocol):
    """Boundary implemented by each upstream filing source."""

    provider: str

    def validate_target(self, target: DiscoveryTarget) -> None:
        """Reject a target this source cannot discover."""

    def discover(self, request: DiscoveryRequest) -> list[FilingReference]:
        """Return filing references matching a homogeneous source request."""


class UnsupportedDiscoverySourceError(ValueError):
    """Raised when no implementation is registered for a configured source."""


class UnsupportedFilingSelectionError(ValueError):
    """Raised when a source cannot process a configured filing route."""


class InvalidDiscoverySourceResultError(RuntimeError):
    """Raised when a source emits a reference outside its assigned targets."""


class ConflictingFilingReferenceError(RuntimeError):
    """Raised when a provider returns inconsistent data for one filing ID."""


class DiscoveryService:
    """Route targets by source and produce one deterministic filing list."""

    def __init__(self, sources: Iterable[FilingDiscoverySource]) -> None:
        self._sources: dict[str, FilingDiscoverySource] = {}
        for source in sources:
            if not source.provider.strip():
                raise ValueError("discovery source provider must not be empty")
            if source.provider in self._sources:
                raise ValueError(f"duplicate discovery source: {source.provider!r}")
            self._sources[source.provider] = source
        if not self._sources:
            raise ValueError("at least one discovery source is required")

    def execute(self, request: DiscoveryRequest) -> DiscoveryResult:
        """Validate, route, deduplicate, and deterministically order filings."""
        targets_by_provider: defaultdict[str, list[DiscoveryTarget]] = defaultdict(list)
        for target in request.targets:
            targets_by_provider[target.issuer.provider].append(target)

        resolved_sources: dict[str, FilingDiscoverySource] = {}
        for provider, provider_targets in targets_by_provider.items():
            source = self._source_for(provider)
            for target in provider_targets:
                source.validate_target(target)
            resolved_sources[provider] = source

        unique: dict[tuple[str, str], FilingReference] = {}
        for provider in sorted(targets_by_provider):
            source_targets = tuple(targets_by_provider[provider])
            source_request = DiscoveryRequest(
                targets=source_targets,
                filed_from=request.filed_from,
                filed_to=request.filed_to,
            )
            allowed_results: set[tuple[str, str, DocumentPolicy]] = {
                (
                    target.company_id,
                    selection.filing_type,
                    selection.document_policy,
                )
                for target in source_targets
                for selection in target.selections
            }
            for filing in resolved_sources[provider].discover(source_request):
                self._validate_source_result(
                    filing,
                    provider=provider,
                    allowed_results=allowed_results,
                    filed_from=request.filed_from,
                    filed_to=request.filed_to,
                )
                key = (filing.provider, filing.provider_filing_id)
                existing = unique.get(key)
                if existing is not None and existing != filing:
                    raise ConflictingFilingReferenceError(
                        f"conflicting references returned for {key[0]}:{key[1]}"
                    )
                unique[key] = filing

        filings = tuple(
            sorted(
                unique.values(),
                key=lambda filing: (
                    filing.filed_on,
                    filing.provider,
                    filing.provider_filing_id,
                ),
            )
        )
        return DiscoveryResult(
            filings=filings,
            issuers_scanned=len(request.targets),
        )

    def _source_for(self, provider: str) -> FilingDiscoverySource:
        try:
            return self._sources[provider]
        except KeyError as error:
            raise UnsupportedDiscoverySourceError(
                f"no discovery source is configured for provider {provider!r}"
            ) from error

    @staticmethod
    def _validate_source_result(
        filing: FilingReference,
        *,
        provider: str,
        allowed_results: set[tuple[str, str, DocumentPolicy]],
        filed_from: date,
        filed_to: date,
    ) -> None:
        if filing.provider != provider:
            raise InvalidDiscoverySourceResultError(
                f"source {provider!r} returned provider {filing.provider!r}"
            )
        selection = (
            filing.company_id,
            filing.filing_type,
            filing.document_policy,
        )
        if selection not in allowed_results:
            raise InvalidDiscoverySourceResultError(
                f"source {provider!r} returned an unrequested filing selection: "
                f"{filing.company_id}:{filing.filing_type}:"
                f"{filing.document_policy.value}"
            )
        if not filed_from <= filing.filed_on <= filed_to:
            raise InvalidDiscoverySourceResultError(
                f"source {provider!r} returned filing outside the requested window: "
                f"{filing.provider_filing_id}:{filing.filed_on.isoformat()}"
            )
