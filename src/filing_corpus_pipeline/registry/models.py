"""State transitions and values stored in the filing registry."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from string import hexdigits
from urllib.parse import quote

from filing_corpus_pipeline.domain import FilingReference


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


def _require_aware(value: datetime, *, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone offset")


def _require_nonempty(value: str, *, field: str) -> None:
    if not value.strip():
        raise ValueError(f"{field} must not be empty")


@dataclass(frozen=True, slots=True)
class ClaimRequest:
    """Request to atomically acquire or recover one filing."""

    filing: FilingReference
    owner_id: str
    claimed_at: datetime
    lease_duration: timedelta

    def __post_init__(self) -> None:
        _require_nonempty(self.owner_id, field="owner_id")
        _require_aware(self.claimed_at, field="claimed_at")
        if self.lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        if self.lease_duration > timedelta(days=1):
            raise ValueError("lease_duration must not exceed one day")

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


@dataclass(frozen=True, slots=True)
class ClaimResult:
    """Decision returned by an atomic registry claim."""

    filing_key: str
    outcome: ClaimOutcome
    status: RegistryStatus
    attempt_count: int
    owner_id: str | None = None

    @property
    def acquired(self) -> bool:
        """Whether the caller owns the filing after this operation."""
        return self.outcome in {ClaimOutcome.CLAIMED, ClaimOutcome.RECLAIMED}


@dataclass(frozen=True, slots=True)
class RawDocumentMetadata:
    """Durable location and integrity metadata for an acquired document."""

    bucket: str
    key: str
    sha256: str
    content_length: int
    content_type: str
    version_id: str | None = None
    etag: str | None = None

    def __post_init__(self) -> None:
        for field in ("bucket", "key", "content_type"):
            _require_nonempty(str(getattr(self, field)), field=field)
        if len(self.sha256) != 64 or any(
            character not in hexdigits for character in self.sha256
        ):
            raise ValueError("sha256 must be a 64-character hexadecimal digest")
        if self.content_length < 0:
            raise ValueError("content_length must not be negative")
        for field in ("version_id", "etag"):
            value = getattr(self, field)
            if value is not None:
                _require_nonempty(value, field=field)

    def to_dict(self) -> dict[str, str | int | None]:
        """Return the workflow-safe durable object identity."""
        return {
            "bucket": self.bucket,
            "key": self.key,
            "sha256": self.sha256.lower(),
            "content_length": self.content_length,
            "content_type": self.content_type,
            "version_id": self.version_id,
            "etag": self.etag,
        }


@dataclass(frozen=True, slots=True)
class NormalizedCorpusMetadata:
    """Durable identity and quality summary for one normalized corpus version."""

    bucket: str
    prefix: str
    manifest_key: str
    manifest_sha256: str
    blocks_key: str
    blocks_sha256: str
    parser_version: str
    schema_version: str
    block_count: int
    section_count: int
    warning_count: int
    quality_status: str

    def __post_init__(self) -> None:
        for field in (
            "bucket",
            "prefix",
            "manifest_key",
            "blocks_key",
            "parser_version",
            "schema_version",
            "quality_status",
        ):
            _require_nonempty(str(getattr(self, field)), field=field)
        for field in ("manifest_sha256", "blocks_sha256"):
            digest = str(getattr(self, field))
            if len(digest) != 64 or any(
                character not in hexdigits for character in digest
            ):
                raise ValueError(f"{field} must be a 64-character hexadecimal digest")
        for field in ("block_count", "section_count", "warning_count"):
            if getattr(self, field) < 0:
                raise ValueError(f"{field} must not be negative")

    def to_dict(self) -> dict[str, str | int]:
        """Return the workflow-safe corpus identity and summary."""
        return {
            "bucket": self.bucket,
            "prefix": self.prefix,
            "manifest_key": self.manifest_key,
            "manifest_sha256": self.manifest_sha256.lower(),
            "blocks_key": self.blocks_key,
            "blocks_sha256": self.blocks_sha256.lower(),
            "parser_version": self.parser_version,
            "schema_version": self.schema_version,
            "block_count": self.block_count,
            "section_count": self.section_count,
            "warning_count": self.warning_count,
            "quality_status": self.quality_status,
        }


@dataclass(frozen=True, slots=True)
class NormalizationClaimRequest:
    """Request to atomically acquire normalization work for one raw filing."""

    filing_key: str
    owner_id: str
    claimed_at: datetime
    lease_duration: timedelta
    parser_version: str

    def __post_init__(self) -> None:
        for field in ("filing_key", "owner_id", "parser_version"):
            _require_nonempty(str(getattr(self, field)), field=field)
        _require_aware(self.claimed_at, field="claimed_at")
        if self.lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        if self.lease_duration > timedelta(days=1):
            raise ValueError("lease_duration must not exceed one day")

    @property
    def lease_expires_at(self) -> datetime:
        return self.claimed_at.astimezone(UTC) + self.lease_duration


@dataclass(frozen=True, slots=True)
class NormalizationClaimResult:
    """Decision and source metadata returned by a normalization claim."""

    filing_key: str
    outcome: ClaimOutcome
    status: RegistryStatus
    attempt_count: int
    raw_document: RawDocumentMetadata
    corpus: NormalizedCorpusMetadata | None = None
    owner_id: str | None = None

    @property
    def acquired(self) -> bool:
        return self.outcome in {ClaimOutcome.CLAIMED, ClaimOutcome.RECLAIMED}


@dataclass(frozen=True, slots=True)
class MarkRawStoredRequest:
    """Request to complete acquisition while still holding its claim."""

    filing_key: str
    owner_id: str
    stored_at: datetime
    document: RawDocumentMetadata

    def __post_init__(self) -> None:
        _require_nonempty(self.filing_key, field="filing_key")
        _require_nonempty(self.owner_id, field="owner_id")
        _require_aware(self.stored_at, field="stored_at")


@dataclass(frozen=True, slots=True)
class FailureDetails:
    """Bounded error information retained for recovery and diagnostics."""

    code: str
    message: str
    retryable: bool

    def __post_init__(self) -> None:
        _require_nonempty(self.code, field="failure code")
        _require_nonempty(self.message, field="failure message")
        if len(self.code) > 100:
            raise ValueError("failure code must not exceed 100 characters")
        if len(self.message) > 1000:
            raise ValueError("failure message must not exceed 1000 characters")


@dataclass(frozen=True, slots=True)
class MarkFailedRequest:
    """Request to release a claim and make a filing recoverable."""

    filing_key: str
    owner_id: str
    failed_at: datetime
    failure: FailureDetails

    def __post_init__(self) -> None:
        _require_nonempty(self.filing_key, field="filing_key")
        _require_nonempty(self.owner_id, field="owner_id")
        _require_aware(self.failed_at, field="failed_at")


@dataclass(frozen=True, slots=True)
class MarkNormalizedRequest:
    """Request to publish a corpus while still holding its normalization claim."""

    filing_key: str
    owner_id: str
    normalized_at: datetime
    corpus: NormalizedCorpusMetadata

    def __post_init__(self) -> None:
        _require_nonempty(self.filing_key, field="filing_key")
        _require_nonempty(self.owner_id, field="owner_id")
        _require_aware(self.normalized_at, field="normalized_at")


@dataclass(frozen=True, slots=True)
class MarkNormalizationFailedRequest:
    """Request to release normalization work and retain failure diagnostics."""

    filing_key: str
    owner_id: str
    failed_at: datetime
    parser_version: str
    failure: FailureDetails

    def __post_init__(self) -> None:
        for field in ("filing_key", "owner_id", "parser_version"):
            _require_nonempty(str(getattr(self, field)), field=field)
        _require_aware(self.failed_at, field="failed_at")
