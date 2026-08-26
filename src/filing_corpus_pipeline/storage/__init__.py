"""Storage clients used by feature services."""

from filing_corpus_pipeline.storage.dynamodb import (
    ConditionalWriteFailed,
    DynamoDbRegistryClient,
    DynamoDbStorageError,
    InvalidDynamoDbItemError,
    StoredNormalizationItem,
    StoredRegistryItem,
)
from filing_corpus_pipeline.storage.s3 import (
    NormalizedCorpusWrite,
    NormalizedObjectCollisionError,
    NormalizedObjectStorageError,
    RawObjectCollisionError,
    RawObjectIntegrityError,
    RawObjectStorageError,
    RawObjectWrite,
    S3NormalizedCorpusClient,
    S3RawDocumentClient,
    StoredNormalizedCorpus,
    StoredRawObject,
)

__all__ = [
    "ConditionalWriteFailed",
    "DynamoDbRegistryClient",
    "DynamoDbStorageError",
    "InvalidDynamoDbItemError",
    "NormalizedCorpusWrite",
    "NormalizedObjectCollisionError",
    "NormalizedObjectStorageError",
    "RawObjectCollisionError",
    "RawObjectIntegrityError",
    "RawObjectStorageError",
    "RawObjectWrite",
    "S3NormalizedCorpusClient",
    "S3RawDocumentClient",
    "StoredNormalizationItem",
    "StoredNormalizedCorpus",
    "StoredRawObject",
    "StoredRegistryItem",
]
