"""Concrete filing registry service and state-transition policy."""

from collections.abc import Callable

from filing_corpus_pipeline.registry.models import (
    ClaimOutcome,
    ClaimRequest,
    ClaimResult,
    MarkFailedRequest,
    MarkRawStoredRequest,
    RegistryStatus,
)
from filing_corpus_pipeline.storage.dynamodb import (
    ConditionalWriteFailed,
    DynamoDbRegistryClient,
    DynamoDbStorageError,
    InvalidDynamoDbItemError,
    StoredRegistryItem,
)


class FilingRegistryError(RuntimeError):
    """Base error for filing registry operations."""


class RegistryLeaseLostError(FilingRegistryError):
    """Raised when a worker tries to finish work it no longer owns."""


class InvalidRegistryItemError(FilingRegistryError):
    """Raised when persisted registry state violates the feature schema."""


class RegistryConsistencyError(FilingRegistryError):
    """Raised when a concurrent claim cannot be classified consistently."""


class FilingRegistryService:
    """Coordinate filing claims and acquisition state transitions."""

    def __init__(self, client: DynamoDbRegistryClient) -> None:
        self._client = client

    def claim(self, request: ClaimRequest) -> ClaimResult:
        """Atomically create, recover, or classify a filing claim."""
        for conflict_number in range(2):
            try:
                previous = self._client.claim(request)
            except ConditionalWriteFailed:
                current = self._get_current(request.filing_key)
                classified = self._classify_conflict(
                    filing_key=request.filing_key,
                    current=current,
                )
                if classified is not None:
                    return classified
                if conflict_number == 1:
                    break
                continue
            except InvalidDynamoDbItemError as error:
                raise InvalidRegistryItemError(str(error)) from error
            except DynamoDbStorageError as error:
                raise FilingRegistryError(
                    "filing claim storage operation failed"
                ) from error

            previous_attempts = previous.attempt_count if previous is not None else 0
            outcome = (
                ClaimOutcome.CLAIMED if previous is None else ClaimOutcome.RECLAIMED
            )
            return ClaimResult(
                filing_key=request.filing_key,
                outcome=outcome,
                status=RegistryStatus.FETCHING,
                attempt_count=previous_attempts + 1,
                owner_id=request.owner_id,
            )

        raise RegistryConsistencyError(
            f"could not classify concurrent claim for {request.filing_key!r}"
        )

    def mark_raw_stored(self, request: MarkRawStoredRequest) -> None:
        """Complete acquisition if the caller still owns the claim."""
        self._complete_owned_transition(
            lambda: self._client.mark_raw_stored(request),
            owner_id=request.owner_id,
        )

    def mark_failed(self, request: MarkFailedRequest) -> None:
        """Record a failed acquisition if the caller still owns the claim."""
        self._complete_owned_transition(
            lambda: self._client.mark_failed(request),
            owner_id=request.owner_id,
        )

    @staticmethod
    def _complete_owned_transition(
        operation: Callable[[], None],
        *,
        owner_id: str,
    ) -> None:
        try:
            operation()
        except ConditionalWriteFailed as error:
            raise RegistryLeaseLostError(
                f"registry claim is no longer owned by {owner_id!r}"
            ) from error
        except DynamoDbStorageError as error:
            raise FilingRegistryError("registry storage update failed") from error

    def _get_current(self, filing_key: str) -> StoredRegistryItem | None:
        try:
            return self._client.get(filing_key)
        except InvalidDynamoDbItemError as error:
            raise InvalidRegistryItemError(str(error)) from error
        except DynamoDbStorageError as error:
            raise FilingRegistryError("claim conflict lookup failed") from error

    @staticmethod
    def _classify_conflict(
        *,
        filing_key: str,
        current: StoredRegistryItem | None,
    ) -> ClaimResult | None:
        if current is None:
            return None
        try:
            status = RegistryStatus(current.status)
        except ValueError as error:
            raise InvalidRegistryItemError(
                f"unsupported registry status: {current.status!r}"
            ) from error

        if status is RegistryStatus.FAILED:
            if current.retryable is None:
                raise InvalidRegistryItemError(
                    "FAILED registry item is missing 'retryable'"
                )
            if current.retryable:
                return None
            return ClaimResult(
                filing_key=filing_key,
                outcome=ClaimOutcome.NOT_RETRYABLE,
                status=status,
                attempt_count=current.attempt_count,
            )
        if status is RegistryStatus.RAW_STORED:
            return ClaimResult(
                filing_key=filing_key,
                outcome=ClaimOutcome.ALREADY_COMPLETED,
                status=status,
                attempt_count=current.attempt_count,
            )
        if status is RegistryStatus.FETCHING:
            if current.owner_id is None:
                raise InvalidRegistryItemError(
                    "FETCHING registry item is missing 'claim_owner'"
                )
            return ClaimResult(
                filing_key=filing_key,
                outcome=ClaimOutcome.ALREADY_IN_PROGRESS,
                status=status,
                attempt_count=current.attempt_count,
                owner_id=current.owner_id,
            )
        raise AssertionError(f"unhandled registry status: {status}")
