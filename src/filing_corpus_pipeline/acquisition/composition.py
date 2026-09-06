"""AWS runtime composition for raw filing acquisition."""

from typing import cast

from filing_corpus_pipeline.acquisition.sec import (
    SecDocumentConfig,
    SecFilingDocumentSource,
)
from filing_corpus_pipeline.acquisition.service import AcquisitionService
from filing_corpus_pipeline.registry import FilingRegistryService
from filing_corpus_pipeline.runtime.aws import aws_client
from filing_corpus_pipeline.runtime.config import required_environment
from filing_corpus_pipeline.sources.http import UrllibHttpTransport
from filing_corpus_pipeline.sources.sec import SecEdgarClient, SecEdgarClientConfig
from filing_corpus_pipeline.storage.dynamodb import (
    DynamoDbApi,
    DynamoDbRegistryClient,
)
from filing_corpus_pipeline.storage.raw_documents import S3RawDocumentClient
from filing_corpus_pipeline.storage.s3 import S3Api


def build_acquisition_service(
    *,
    registry_table_name: str,
    raw_bucket_name: str,
    max_document_bytes: int,
    max_filing_detail_bytes: int,
    sec_user_agent: str | None = None,
) -> AcquisitionService:
    """Compose storage and all document sources enabled in this runtime."""
    user_agent = (
        sec_user_agent
        if sec_user_agent is not None
        else required_environment("SEC_USER_AGENT")
    )
    dynamodb = DynamoDbRegistryClient(
        cast(DynamoDbApi, aws_client("dynamodb")),
        table_name=registry_table_name,
    )
    raw_storage = S3RawDocumentClient(
        cast(S3Api, aws_client("s3")),
        bucket_name=raw_bucket_name,
    )
    source = SecFilingDocumentSource(
        client=SecEdgarClient(
            transport=UrllibHttpTransport(),
            config=SecEdgarClientConfig(
                user_agent=user_agent,
                timeout_seconds=20.0,
            ),
        ),
        config=SecDocumentConfig(
            max_document_bytes=max_document_bytes,
            max_filing_detail_bytes=max_filing_detail_bytes,
        ),
    )
    return AcquisitionService(
        registry=FilingRegistryService(dynamodb),
        raw_storage=raw_storage,
        sources=[source],
    )
