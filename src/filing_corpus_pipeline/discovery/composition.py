"""Runtime composition for the filing discovery feature."""

from typing import cast

from filing_corpus_pipeline.discovery.sec import SecFilingDiscoverySource
from filing_corpus_pipeline.discovery.service import DiscoveryService
from filing_corpus_pipeline.discovery.target_repository import (
    DiscoveryTargetRepository,
)
from filing_corpus_pipeline.runtime.aws import aws_client
from filing_corpus_pipeline.sources.http import UrllibHttpTransport
from filing_corpus_pipeline.sources.sec import SecEdgarClient, SecEdgarClientConfig
from filing_corpus_pipeline.storage.s3 import S3ObjectClient, S3ReadApi


def build_sec_discovery_service(user_agent: str) -> DiscoveryService:
    """Compose the SEC client and discovery-specific listing behavior."""
    client = SecEdgarClient(
        transport=UrllibHttpTransport(),
        config=SecEdgarClientConfig(user_agent=user_agent),
    )
    return DiscoveryService(SecFilingDiscoverySource(client))


def build_discovery_target_repository() -> DiscoveryTargetRepository:
    """Compose the S3-backed repository for deployed discovery targets."""
    return DiscoveryTargetRepository(
        S3ObjectClient(cast(S3ReadApi, aws_client("s3"))),
    )
