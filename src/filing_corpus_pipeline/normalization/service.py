"""Normalization orchestration from a durable raw object to a committed corpus."""

from collections.abc import Callable
from datetime import UTC, datetime

from filing_corpus_pipeline.normalization.artifacts import (
    normalized_document_prefix,
    render_artifacts,
)
from filing_corpus_pipeline.normalization.models import (
    DocumentParseError,
    NormalizationOutcome,
    NormalizationRequest,
    NormalizationResult,
    RawFilingDocument,
)
from filing_corpus_pipeline.normalization.sec_html import SecHtmlNormalizer
from filing_corpus_pipeline.registry import (
    ClaimOutcome,
    FailureDetails,
    FilingRegistryService,
    MarkNormalizationFailedRequest,
    MarkNormalizedRequest,
    NormalizationClaimRequest,
    NormalizedCorpusMetadata,
    filing_registry_key,
)
from filing_corpus_pipeline.storage import (
    NormalizedCorpusWrite,
    NormalizedObjectStorageError,
    RawObjectStorageError,
    S3NormalizedCorpusClient,
    S3RawDocumentClient,
)


class NormalizationError(RuntimeError):
    """A recorded normalization failure exposed to the runtime entrypoint."""

    def __init__(self, message: str, *, code: str, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class RetryableNormalizationError(NormalizationError):
    """A recorded failure that Step Functions may safely retry."""


class PermanentNormalizationError(NormalizationError):
    """A recorded failure that should not consume workflow retries."""


class NormalizationService:
    """Claim, load, parse, publish, and finalize one raw filing."""

    def __init__(
        self,
        *,
        registry: FilingRegistryService,
        raw_storage: S3RawDocumentClient,
        corpus_storage: S3NormalizedCorpusClient,
        normalizer: SecHtmlNormalizer,
        max_document_bytes: int,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if max_document_bytes < 1:
            raise ValueError("max_document_bytes must be positive")
        self._registry = registry
        self._raw_storage = raw_storage
        self._corpus_storage = corpus_storage
        self._normalizer = normalizer
        self._max_document_bytes = max_document_bytes
        self._clock = clock or (lambda: datetime.now(UTC))

    def normalize(self, request: NormalizationRequest) -> NormalizationResult:
        """Normalize a filing or return its existing registry disposition."""
        filing_key = filing_registry_key(
            request.filing.provider,
            request.filing.provider_filing_id,
        )
        claim = self._registry.claim_normalization(
            NormalizationClaimRequest(
                filing_key=filing_key,
                owner_id=request.owner_id,
                claimed_at=request.requested_at,
                lease_duration=request.lease_duration,
                parser_version=self._normalizer.parser_version,
            )
        )
        if not claim.acquired:
            return NormalizationResult(
                filing_key=claim.filing_key,
                outcome=_duplicate_outcome(claim.outcome),
                attempt_count=claim.attempt_count,
                corpus=claim.corpus,
            )

        try:
            body = self._raw_storage.load(
                claim.raw_document,
                max_bytes=self._max_document_bytes,
            )
            document = self._normalizer.normalize(
                RawFilingDocument(
                    filing_key=claim.filing_key,
                    filing=request.filing,
                    body=body,
                    content_type=claim.raw_document.content_type,
                    expected_sha256=claim.raw_document.sha256.lower(),
                )
            )
            artifacts = render_artifacts(document)
            try:
                prefix = normalized_document_prefix(document)
            except ValueError as error:
                raise DocumentParseError(
                    str(error),
                    code="INVALID_NORMALIZED_OBJECT_KEY",
                ) from error
            stored = self._corpus_storage.store(
                NormalizedCorpusWrite(
                    prefix=prefix,
                    manifest=artifacts.manifest,
                    manifest_sha256=artifacts.manifest_sha256,
                    blocks=artifacts.blocks_jsonl_gzip,
                    blocks_sha256=artifacts.blocks_sha256,
                    filing_key=claim.filing_key,
                    parser_version=document.parser_version,
                    source_sha256=document.source_sha256,
                )
            )
        except (
            DocumentParseError,
            RawObjectStorageError,
            NormalizedObjectStorageError,
        ) as error:
            self._record_failure(
                filing_key=claim.filing_key,
                owner_id=request.owner_id,
                error=error,
            )
            error_type = (
                RetryableNormalizationError
                if error.retryable
                else PermanentNormalizationError
            )
            raise error_type(
                str(error),
                code=error.code,
                retryable=error.retryable,
            ) from error

        corpus = NormalizedCorpusMetadata(
            bucket=stored.bucket,
            prefix=stored.prefix,
            manifest_key=stored.manifest_key,
            manifest_sha256=artifacts.manifest_sha256,
            blocks_key=stored.blocks_key,
            blocks_sha256=artifacts.blocks_sha256,
            parser_version=document.parser_version,
            schema_version=document.schema_version,
            block_count=len(document.blocks),
            section_count=len(document.sections),
            warning_count=len(document.warnings),
            quality_status=document.quality_status.value,
        )
        self._registry.mark_normalized(
            MarkNormalizedRequest(
                filing_key=claim.filing_key,
                owner_id=request.owner_id,
                normalized_at=self._clock(),
                corpus=corpus,
            )
        )
        return NormalizationResult(
            filing_key=claim.filing_key,
            outcome=NormalizationOutcome.NORMALIZED,
            attempt_count=claim.attempt_count,
            corpus=corpus,
        )

    def _record_failure(
        self,
        *,
        filing_key: str,
        owner_id: str,
        error: (
            DocumentParseError | RawObjectStorageError | NormalizedObjectStorageError
        ),
    ) -> None:
        self._registry.mark_normalization_failed(
            MarkNormalizationFailedRequest(
                filing_key=filing_key,
                owner_id=owner_id,
                failed_at=self._clock(),
                parser_version=self._normalizer.parser_version,
                failure=FailureDetails(
                    code=error.code,
                    message=str(error),
                    retryable=error.retryable,
                ),
            )
        )


def _duplicate_outcome(outcome: ClaimOutcome) -> NormalizationOutcome:
    mapping = {
        ClaimOutcome.ALREADY_COMPLETED: NormalizationOutcome.ALREADY_COMPLETED,
        ClaimOutcome.ALREADY_IN_PROGRESS: NormalizationOutcome.ALREADY_IN_PROGRESS,
        ClaimOutcome.NOT_RETRYABLE: NormalizationOutcome.NOT_RETRYABLE,
    }
    try:
        return mapping[outcome]
    except KeyError as error:
        raise AssertionError(
            f"acquired normalization claim was not handled: {outcome}"
        ) from error
