"""State transitions and values stored in the filing registry."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from string import hexdigits
from urllib.parse import quote

from filing_corpus_pipeline.domain import FilingReference


class RegistryStatus(StrEnum):
    """Acquisition states persisted for a filing."""

    FETCHING = "FETCHING"
    RAW_STORED = "RAW_STORED"
    FAILED = "FAILED"


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
