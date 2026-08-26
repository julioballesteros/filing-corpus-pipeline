"""State transitions and values stored in the filing registry."""

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from urllib.parse import quote

from pydantic import AwareDatetime, Field, field_validator

from filing_corpus_pipeline.domain import FilingReference
from filing_corpus_pipeline.models import (
    NonEmptyString,
    PipelineModel,
    Sha256Digest,
)


class RegistryStatus(StrEnum):
    """Lifecycle states persisted for a filing."""

    FETCHING = "FETCHING"
    RAW_STORED = "RAW_STORED"
    FAILED = "FAILED"
    NORMALIZING = "NORMALIZING"
    NORMALIZED = "NORMALIZED"
    NORMALIZATION_FAILED = "NORMALIZATION_FAILED"


class ClaimOutcome(StrEnum):
    """Result of attempting to acquire exclusive work on a filing."""

    CLAIMED = "CLAIMED"
    RECLAIMED = "RECLAIMED"
    ALREADY_COMPLETED = "ALREADY_COMPLETED"
    ALREADY_IN_PROGRESS = "ALREADY_IN_PROGRESS"
    NOT_RETRYABLE = "NOT_RETRYABLE"


def filing_registry_key(provider: str, provider_filing_id: str) -> str:
    """Build a stable, collision-safe key in the provider's ID namespace."""
    if not provider.strip():
        raise ValueError("provider must not be empty")
    if not provider_filing_id.strip():
        raise ValueError("provider_filing_id must not be empty")
    key = f"{quote(provider, safe='')}#{quote(provider_filing_id, safe='')}"
    if len(key.encode()) > 2048:
        raise ValueError("filing registry key exceeds DynamoDB's key size limit")
    return key


class ClaimRequest(PipelineModel):
    """Request to atomically acquire or recover one filing."""

    filing: FilingReference
    owner_id: NonEmptyString
    claimed_at: AwareDatetime
    lease_duration: timedelta = Field(
        gt=timedelta(0),
        le=timedelta(days=1),
    )

    @property
    def filing_key(self) -> str:
        """Return the filing's DynamoDB partition key."""
        return filing_registry_key(
            self.filing.provider,
            self.filing.provider_filing_id,
        )

    @property
    def lease_expires_at(self) -> datetime:
        """Return the UTC instant after which another owner may recover work."""
        return self.claimed_at.astimezone(UTC) + self.lease_duration


class ClaimResult(PipelineModel):
    """Decision returned by an atomic registry claim."""

    filing_key: NonEmptyString
    outcome: ClaimOutcome
    status: RegistryStatus
    attempt_count: int = Field(ge=0)
    owner_id: str | None = None

    @property
    def acquired(self) -> bool:
        """Whether the caller owns the filing after this operation."""
        return self.outcome in {ClaimOutcome.CLAIMED, ClaimOutcome.RECLAIMED}


class RawDocumentMetadata(PipelineModel):
    """Durable location and integrity metadata for an acquired document."""

    bucket: NonEmptyString
    key: NonEmptyString
    sha256: Sha256Digest
    content_length: int = Field(ge=0)
    content_type: NonEmptyString
    version_id: str | None = None
    etag: str | None = None

    @field_validator("version_id", "etag")
    @classmethod
    def _validate_optional_identity(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("must not be empty")
        return value


class NormalizedCorpusMetadata(PipelineModel):
    """Durable identity and quality summary for one normalized corpus version."""

    bucket: NonEmptyString
    prefix: NonEmptyString
    manifest_key: NonEmptyString
    manifest_sha256: Sha256Digest
    blocks_key: NonEmptyString
    blocks_sha256: Sha256Digest
    parser_version: NonEmptyString
    schema_version: NonEmptyString
    block_count: int = Field(ge=0)
    section_count: int = Field(ge=0)
    warning_count: int = Field(ge=0)
    quality_status: NonEmptyString


class NormalizationClaimRequest(PipelineModel):
    """Request to atomically acquire normalization work for one raw filing."""

    filing_key: NonEmptyString
    owner_id: NonEmptyString
    claimed_at: AwareDatetime
    lease_duration: timedelta = Field(
        gt=timedelta(0),
        le=timedelta(days=1),
    )
    parser_version: NonEmptyString

    @property
    def lease_expires_at(self) -> datetime:
        return self.claimed_at.astimezone(UTC) + self.lease_duration


class NormalizationClaimResult(PipelineModel):
    """Decision and source metadata returned by a normalization claim."""

    filing_key: NonEmptyString
    outcome: ClaimOutcome
    status: RegistryStatus
    attempt_count: int = Field(ge=0)
    raw_document: RawDocumentMetadata
    corpus: NormalizedCorpusMetadata | None = None
    owner_id: str | None = None

    @property
    def acquired(self) -> bool:
        return self.outcome in {ClaimOutcome.CLAIMED, ClaimOutcome.RECLAIMED}


class MarkRawStoredRequest(PipelineModel):
    """Request to complete acquisition while still holding its claim."""

    filing_key: NonEmptyString
    owner_id: NonEmptyString
    stored_at: AwareDatetime
    document: RawDocumentMetadata


class FailureDetails(PipelineModel):
    """Bounded error information retained for recovery and diagnostics."""

    code: NonEmptyString = Field(max_length=100)
    message: NonEmptyString = Field(max_length=1000)
    retryable: bool


class MarkFailedRequest(PipelineModel):
    """Request to release a claim and make a filing recoverable."""

    filing_key: NonEmptyString
    owner_id: NonEmptyString
    failed_at: AwareDatetime
    failure: FailureDetails


class MarkNormalizedRequest(PipelineModel):
    """Request to publish a corpus while still holding its normalization claim."""

    filing_key: NonEmptyString
    owner_id: NonEmptyString
    normalized_at: AwareDatetime
    corpus: NormalizedCorpusMetadata


class MarkNormalizationFailedRequest(PipelineModel):
    """Request to release normalization work and retain failure diagnostics."""

    filing_key: NonEmptyString
    owner_id: NonEmptyString
    failed_at: AwareDatetime
    parser_version: NonEmptyString
    failure: FailureDetails
