"""AWS runtime composition for raw filing acquisition."""

from importlib import import_module
from typing import Protocol, cast

from filing_corpus_pipeline.acquisition.service import AcquisitionService
from filing_corpus_pipeline.adapters.http import UrllibBytesTransport
from filing_corpus_pipeline.adapters.sec.documents import (
    SecDocumentConfig,
    SecFilingDocumentSource,
)
from filing_corpus_pipeline.registry import FilingRegistryService
from filing_corpus_pipeline.storage.dynamodb import (
    DynamoDbApi,
    DynamoDbRegistryClient,
)
from filing_corpus_pipeline.storage.s3 import S3Api, S3RawDocumentClient


class _AwsClientFactory(Protocol):
    """Shape of the boto3 module used only at the runtime composition root."""

    def client(self, service_name: str) -> object:
        """Create one low-level AWS service client."""


def build_sec_acquisition_service(
    *,
    user_agent: str,
    registry_table_name: str,
    raw_bucket_name: str,
    max_document_bytes: int,
) -> AcquisitionService:
    """Wire the SEC source to concrete DynamoDB and S3 storage clients."""
    dynamodb = DynamoDbRegistryClient(
        cast(DynamoDbApi, _aws_client("dynamodb")),
        table_name=registry_table_name,
    )
    raw_storage = S3RawDocumentClient(
        cast(S3Api, _aws_client("s3")),
        bucket_name=raw_bucket_name,
    )
    source = SecFilingDocumentSource(
        transport=UrllibBytesTransport(),
        config=SecDocumentConfig(
            user_agent=user_agent,
            max_document_bytes=max_document_bytes,
        ),
    )
    return AcquisitionService(
        registry=FilingRegistryService(dynamodb),
        raw_storage=raw_storage,
        sources=[source],
    )


def _aws_client(service_name: str) -> object:
    """Use the AWS SDK supplied by the managed Lambda Python runtime."""
    factory = cast(_AwsClientFactory, import_module("boto3"))
    return factory.client(service_name)
