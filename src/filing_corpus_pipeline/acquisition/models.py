"""Provider-neutral values for acquiring one raw filing document."""

from datetime import timedelta
from enum import StrEnum
from urllib.parse import quote

from pydantic import AwareDatetime, Field, model_validator

from filing_corpus_pipeline.domain import FilingReference
from filing_corpus_pipeline.models import NonEmptyString, PipelineModel
from filing_corpus_pipeline.registry import RawDocumentMetadata


def raw_document_key(filing: FilingReference) -> str:
    """Build the deterministic S3 key for a provider filing document."""
    segments = (
        "raw",
        filing.provider,
        filing.issuer.provider_issuer_id,
        filing.provider_filing_id,
        filing.primary_document,
    )
    key = "/".join(quote(segment, safe="") for segment in segments)
    if len(key.encode()) > 1024:
        raise ValueError("raw document key exceeds S3's key size limit")
    return key


class AcquisitionRequest(PipelineModel):
    """Request to claim and acquire one discovered filing."""

    filing: FilingReference
    owner_id: NonEmptyString
    requested_at: AwareDatetime
    lease_duration: timedelta = Field(
        gt=timedelta(0),
        le=timedelta(days=1),
    )


class RetrievedDocument(PipelineModel):
    """Raw bytes and response metadata returned by a filing source."""

    body: bytes
    content_type: NonEmptyString
    source_url: NonEmptyString
    source_etag: str | None = None
    source_last_modified: str | None = None


class AcquisitionOutcome(StrEnum):
    """Terminal or duplicate-safe result of an acquisition invocation."""

    RAW_STORED = "RAW_STORED"
    ALREADY_COMPLETED = "ALREADY_COMPLETED"
    ALREADY_IN_PROGRESS = "ALREADY_IN_PROGRESS"
    NOT_RETRYABLE = "NOT_RETRYABLE"


class AcquisitionResult(PipelineModel):
    """Small workflow-safe result; document bytes are never included."""

    filing_key: NonEmptyString
    outcome: AcquisitionOutcome
    attempt_count: int = Field(gt=0)
    document: RawDocumentMetadata | None = None

    @model_validator(mode="after")
    def _validate_document_disposition(self) -> "AcquisitionResult":
        if (self.outcome is AcquisitionOutcome.RAW_STORED) != (
            self.document is not None
        ):
            raise ValueError("document metadata is required only for RAW_STORED")
        return self


class DocumentRetrievalError(RuntimeError):
    """Expected provider failure that can be persisted and classified."""

    def __init__(self, message: str, *, code: str, retryable: bool) -> None:
        if not code.strip() or len(code) > 100:
            raise ValueError("retrieval error code must contain at most 100 characters")
        if not message.strip() or len(message) > 1000:
            raise ValueError(
                "retrieval error message must contain at most 1000 characters"
            )
        super().__init__(message)
        self.code = code
        self.retryable = retryable
