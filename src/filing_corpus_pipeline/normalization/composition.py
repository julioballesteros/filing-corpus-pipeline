"""AWS runtime composition for filing normalization."""

from typing import cast

from filing_corpus_pipeline.normalization.models import SecHtmlParserConfig
from filing_corpus_pipeline.normalization.sec_html import SecHtmlNormalizer
from filing_corpus_pipeline.normalization.service import NormalizationService
from filing_corpus_pipeline.registry import FilingRegistryService
from filing_corpus_pipeline.runtime.aws import aws_client
from filing_corpus_pipeline.storage.dynamodb import DynamoDbApi, DynamoDbRegistryClient
from filing_corpus_pipeline.storage.normalized_corpus import S3NormalizedCorpusClient
from filing_corpus_pipeline.storage.raw_documents import S3RawDocumentClient
from filing_corpus_pipeline.storage.s3 import S3Api


def build_sec_normalization_service(
    *,
    registry_table_name: str,
    raw_bucket_name: str,
    normalized_bucket_name: str,
    max_document_bytes: int,
) -> NormalizationService:
    """Wire deterministic SEC HTML normalization to DynamoDB and S3."""
    dynamodb = DynamoDbRegistryClient(
        cast(DynamoDbApi, aws_client("dynamodb")),
        table_name=registry_table_name,
    )
    s3 = cast(S3Api, aws_client("s3"))
    return NormalizationService(
        registry=FilingRegistryService(dynamodb),
        raw_storage=S3RawDocumentClient(s3, bucket_name=raw_bucket_name),
        corpus_storage=S3NormalizedCorpusClient(
            s3,
            bucket_name=normalized_bucket_name,
        ),
        normalizer=SecHtmlNormalizer(
            SecHtmlParserConfig(max_input_bytes=max_document_bytes)
        ),
        max_document_bytes=max_document_bytes,
    )
