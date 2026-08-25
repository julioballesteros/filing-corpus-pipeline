"""Acquisition orchestration from an atomic claim to durable raw bytes."""

from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from hashlib import sha256
from typing import Protocol

from filing_corpus_pipeline.acquisition.models import (
    AcquisitionOutcome,
    AcquisitionRequest,
    AcquisitionResult,
    DocumentRetrievalError,
    RetrievedDocument,
    raw_document_key,
)
from filing_corpus_pipeline.domain import FilingReference
from filing_corpus_pipeline.registry import (
    ClaimOutcome,
    ClaimRequest,
    FailureDetails,
    FilingRegistryService,
    MarkFailedRequest,
    MarkRawStoredRequest,
    RawDocumentMetadata,
)
from filing_corpus_pipeline.storage.s3 import (
    RawObjectStorageError,
    RawObjectWrite,
    S3RawDocumentClient,
)


class FilingDocumentSource(Protocol):
    """Provider boundary for retrieving one discovered filing document."""

    provider: str

    def retrieve(self, filing: FilingReference) -> RetrievedDocument:
        """Retrieve one bounded, byte-for-byte source document."""


class AcquisitionError(RuntimeError):
    """A recorded acquisition failure exposed to the runtime entrypoint."""

    def __init__(self, message: str, *, code: str, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class RetryableAcquisitionError(AcquisitionError):
    """A recorded failure that a workflow may safely retry."""


class PermanentAcquisitionError(AcquisitionError):
    """A recorded failure that should not consume workflow retries."""


class AcquisitionService:
    """Claim, retrieve, durably store, and finalize one filing."""

    def __init__(
        self,
        *,
        registry: FilingRegistryService,
        raw_storage: S3RawDocumentClient,
        sources: Iterable[FilingDocumentSource],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._registry = registry
        self._raw_storage = raw_storage
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sources: dict[str, FilingDocumentSource] = {}
        for source in sources:
            if not source.provider.strip():
                raise ValueError("document source provider must not be empty")
            if source.provider in self._sources:
                raise ValueError(f"duplicate document source: {source.provider!r}")
            self._sources[source.provider] = source
        if not self._sources:
            raise ValueError("at least one document source is required")

    def acquire(self, request: AcquisitionRequest) -> AcquisitionResult:
        """Acquire a filing or return its existing registry disposition."""
        claim = self._registry.claim(
            ClaimRequest(
                filing=request.filing,
                owner_id=request.owner_id,
                claimed_at=request.requested_at,
                lease_duration=request.lease_duration,
            )
        )
        if not claim.acquired:
            return AcquisitionResult(
                filing_key=claim.filing_key,
                outcome=_duplicate_outcome(claim.outcome),
                attempt_count=claim.attempt_count,
            )

        try:
            source = self._source_for(request.filing.provider)
            retrieved = source.retrieve(request.filing)
            if not retrieved.body:
                raise DocumentRetrievalError(
                    "source returned an empty filing document",
                    code="EMPTY_SOURCE_DOCUMENT",
                    retryable=False,
                )

            digest = sha256(retrieved.body).hexdigest()
            stored = self._raw_storage.store(
                RawObjectWrite(
                    key=_validated_object_key(request.filing),
                    body=retrieved.body,
                    sha256=digest,
                    content_type=retrieved.content_type,
                    filing_key=claim.filing_key,
                    source_url=retrieved.source_url,
                    source_etag=retrieved.source_etag,
                    source_last_modified=retrieved.source_last_modified,
                )
            )
        except (DocumentRetrievalError, RawObjectStorageError) as error:
            self._record_failure(
                filing_key=claim.filing_key,
                owner_id=request.owner_id,
                error=error,
            )
            error_type = (
                RetryableAcquisitionError
                if error.retryable
                else PermanentAcquisitionError
            )
            raise error_type(
                str(error),
                code=error.code,
                retryable=error.retryable,
            ) from error

        metadata = RawDocumentMetadata(
            bucket=stored.bucket,
            key=stored.key,
            sha256=digest,
            content_length=len(retrieved.body),
            content_type=retrieved.content_type,
            version_id=stored.version_id,
            etag=stored.etag,
        )
        self._registry.mark_raw_stored(
            MarkRawStoredRequest(
                filing_key=claim.filing_key,
                owner_id=request.owner_id,
                stored_at=self._clock(),
                document=metadata,
            )
        )
        return AcquisitionResult(
            filing_key=claim.filing_key,
            outcome=AcquisitionOutcome.RAW_STORED,
            attempt_count=claim.attempt_count,
            document=metadata,
        )

    def _source_for(self, provider: str) -> FilingDocumentSource:
        try:
            return self._sources[provider]
        except KeyError as error:
            raise DocumentRetrievalError(
                f"no document source is configured for provider {provider!r}",
                code="UNSUPPORTED_PROVIDER",
                retryable=False,
            ) from error

    def _record_failure(
        self,
        *,
        filing_key: str,
        owner_id: str,
        error: DocumentRetrievalError | RawObjectStorageError,
    ) -> None:
        self._registry.mark_failed(
            MarkFailedRequest(
                filing_key=filing_key,
                owner_id=owner_id,
                failed_at=self._clock(),
                failure=FailureDetails(
                    code=error.code,
                    message=str(error),
                    retryable=error.retryable,
                ),
            )
        )


def _duplicate_outcome(outcome: ClaimOutcome) -> AcquisitionOutcome:
    mapping = {
        ClaimOutcome.ALREADY_COMPLETED: AcquisitionOutcome.ALREADY_COMPLETED,
        ClaimOutcome.ALREADY_IN_PROGRESS: AcquisitionOutcome.ALREADY_IN_PROGRESS,
        ClaimOutcome.NOT_RETRYABLE: AcquisitionOutcome.NOT_RETRYABLE,
    }
    try:
        return mapping[outcome]
    except KeyError as error:
        raise AssertionError(f"acquired claim was not handled: {outcome}") from error


def _validated_object_key(filing: FilingReference) -> str:
    try:
        return raw_document_key(filing)
    except ValueError as error:
        raise DocumentRetrievalError(
            str(error),
            code="INVALID_RAW_OBJECT_KEY",
            retryable=False,
        ) from error
