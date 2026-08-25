"""Raw filing acquisition feature."""

from filing_corpus_pipeline.acquisition.models import (
    AcquisitionOutcome,
    AcquisitionRequest,
    AcquisitionResult,
    DocumentRetrievalError,
    RetrievedDocument,
    raw_document_key,
)
from filing_corpus_pipeline.acquisition.service import (
    AcquisitionError,
    AcquisitionService,
    FilingDocumentSource,
    PermanentAcquisitionError,
    RetryableAcquisitionError,
)

__all__ = [
    "AcquisitionError",
    "AcquisitionOutcome",
    "AcquisitionRequest",
    "AcquisitionResult",
    "AcquisitionService",
    "DocumentRetrievalError",
    "FilingDocumentSource",
    "PermanentAcquisitionError",
    "RetrievedDocument",
    "RetryableAcquisitionError",
    "raw_document_key",
]
