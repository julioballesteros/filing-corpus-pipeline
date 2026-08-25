"""Provider-neutral values for acquiring one raw filing document."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from urllib.parse import quote

from filing_corpus_pipeline.domain import FilingReference
from filing_corpus_pipeline.registry import RawDocumentMetadata


def _require_aware(value: datetime, *, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone offset")


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


@dataclass(frozen=True, slots=True)
class AcquisitionRequest:
    """Request to claim and acquire one discovered filing."""

    filing: FilingReference
    owner_id: str
    requested_at: datetime
    lease_duration: timedelta

    def __post_init__(self) -> None:
        if not self.owner_id.strip():
            raise ValueError("owner_id must not be empty")
        _require_aware(self.requested_at, field="requested_at")
        if self.lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        if self.lease_duration > timedelta(days=1):
            raise ValueError("lease_duration must not exceed one day")


@dataclass(frozen=True, slots=True)
class RetrievedDocument:
    """Raw bytes and source response metadata returned by a provider adapter."""

    body: bytes
    content_type: str
    source_url: str
    source_etag: str | None = None
    source_last_modified: str | None = None

    def __post_init__(self) -> None:
        if not self.content_type.strip():
            raise ValueError("content_type must not be empty")
        if not self.source_url.strip():
            raise ValueError("source_url must not be empty")


class AcquisitionOutcome(StrEnum):
    """Terminal or duplicate-safe result of an acquisition invocation."""

    RAW_STORED = "RAW_STORED"
    ALREADY_COMPLETED = "ALREADY_COMPLETED"
    ALREADY_IN_PROGRESS = "ALREADY_IN_PROGRESS"
    NOT_RETRYABLE = "NOT_RETRYABLE"


@dataclass(frozen=True, slots=True)
class AcquisitionResult:
    """Small workflow-safe result; document bytes are never included."""

    filing_key: str
    outcome: AcquisitionOutcome
    attempt_count: int
    document: RawDocumentMetadata | None = None

    def __post_init__(self) -> None:
        if not self.filing_key.strip():
            raise ValueError("filing_key must not be empty")
        if self.attempt_count < 1:
            raise ValueError("attempt_count must be positive")
        if (self.outcome is AcquisitionOutcome.RAW_STORED) != (
            self.document is not None
        ):
            raise ValueError("document metadata is required only for RAW_STORED")


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
