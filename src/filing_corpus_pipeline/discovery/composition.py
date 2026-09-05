"""Runtime composition for the filing discovery feature."""

from importlib import import_module
from typing import Protocol, cast

from filing_corpus_pipeline.adapters.aws.discovery_targets import (
    DiscoveryTargetS3Api,
    S3DiscoveryTargetRepository,
)
from filing_corpus_pipeline.adapters.http import UrllibJsonTransport
from filing_corpus_pipeline.adapters.sec.discovery import SecFilingDiscoverySource
from filing_corpus_pipeline.adapters.sec.submissions import (
    SecClientConfig,
    SecSubmissionsClient,
)
from filing_corpus_pipeline.discovery import DiscoveryService


class _AwsClientFactory(Protocol):
    """Shape of boto3 supplied by the managed Lambda runtime."""

    def client(self, service_name: str) -> object:
        """Create one low-level AWS service client."""


def build_sec_discovery_service(user_agent: str) -> DiscoveryService:
    """Compose the SEC adapter and provider-independent discovery service."""
    client = SecSubmissionsClient(
        transport=UrllibJsonTransport(),
        config=SecClientConfig(user_agent=user_agent),
    )
    return DiscoveryService(SecFilingDiscoverySource(client))


def build_discovery_target_repository() -> S3DiscoveryTargetRepository:
    """Compose the S3-backed repository for deployed discovery targets."""
    return S3DiscoveryTargetRepository(
        cast(DiscoveryTargetS3Api, _aws_client("s3")),
    )


def _aws_client(service_name: str) -> object:
    """Use the AWS SDK supplied by the managed Lambda Python runtime."""
    factory = cast(_AwsClientFactory, import_module("boto3"))
    return factory.client(service_name)
