"""Runtime composition for the filing discovery feature."""

from filing_corpus_pipeline.adapters.http import UrllibJsonTransport
from filing_corpus_pipeline.adapters.sec.discovery import SecFilingDiscoverySource
from filing_corpus_pipeline.adapters.sec.submissions import (
    SecClientConfig,
    SecSubmissionsClient,
)
from filing_corpus_pipeline.discovery import DiscoveryService


def build_sec_discovery_service(user_agent: str) -> DiscoveryService:
    """Compose the SEC adapter and provider-independent discovery service."""
    client = SecSubmissionsClient(
        transport=UrllibJsonTransport(),
        config=SecClientConfig(user_agent=user_agent),
    )
    return DiscoveryService(SecFilingDiscoverySource(client))
