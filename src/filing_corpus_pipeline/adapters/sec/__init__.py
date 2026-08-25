"""SEC EDGAR source adapter."""

from filing_corpus_pipeline.adapters.sec.discovery import SecFilingDiscoverySource
from filing_corpus_pipeline.adapters.sec.submissions import (
    SecClientConfig,
    SecSubmissionsClient,
)

__all__ = [
    "SecClientConfig",
    "SecFilingDiscoverySource",
    "SecSubmissionsClient",
]
