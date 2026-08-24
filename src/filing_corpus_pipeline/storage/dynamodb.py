"""DynamoDB client for filing registry persistence."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, NoReturn, Protocol

if TYPE_CHECKING:
    from filing_corpus_pipeline.registry.models import (
        ClaimRequest,
        MarkFailedRequest,
        MarkRawStoredRequest,
    )

SCHEMA_VERSION = 1

AttributeValue = dict[str, object]
DynamoItem = Mapping[str, object]


class DynamoDbApi(Protocol):
    """Low-level DynamoDB SDK operations used by the storage client."""

    def update_item(self, **kwargs: object) -> Mapping[str, object]:
        """Atomically update one item."""

    def get_item(self, **kwargs: object) -> Mapping[str, object]:
        """Read one item."""


class DynamoDbStorageError(RuntimeError):
    """Raised when DynamoDB cannot complete a storage operation."""


class ConditionalWriteFailed(DynamoDbStorageError):
    """Raised when a DynamoDB condition rejects a state transition."""


class InvalidDynamoDbItemError(DynamoDbStorageError):
    """Raised when a DynamoDB item violates its persisted shape."""


@dataclass(frozen=True, slots=True)
class StoredRegistryItem:
    """Registry fields required by the service after a storage operation."""

    status: str
    attempt_count: int
    owner_id: str | None
    retryable: bool | None


class DynamoDbRegistryClient:
    """Persist and retrieve registry state using DynamoDB expressions."""

    def __init__(self, api: DynamoDbApi, *, table_name: str) -> None:
        if not table_name.strip():
            raise ValueError("table_name must not be empty")
        self._api = api
        self._table_name = table_name

    def claim(self, request: ClaimRequest) -> StoredRegistryItem | None:
        """Attempt one atomic filing claim and return the prior item."""
        try:
            response = self._api.update_item(
                TableName=self._table_name,
                Key={"filing_key": _string(request.filing_key)},
                UpdateExpression=_claim_update_expression(),
                ConditionExpression=(
                    "attribute_not_exists(#filing_key) OR "
                    "(#status = :failed AND #retryable = :true) OR "
                    "(#status = :fetching AND "
                    "(#claim_owner = :owner OR "
                    "attribute_not_exists(#lease_expires_at_epoch) OR "
                    "#lease_expires_at_epoch <= :now_epoch))"
                ),
                ExpressionAttributeNames=_claim_attribute_names(),
                ExpressionAttributeValues=_claim_attribute_values(request),
                ReturnValues="ALL_OLD",
            )
        except Exception as error:
            _raise_storage_error(error, operation="filing claim")

        return _parse_optional_item(response.get("Attributes"), field="Attributes")

    def get(self, filing_key: str) -> StoredRegistryItem | None:
        """Read the state fields used to classify a claim conflict."""
        try:
            response = self._api.get_item(
                TableName=self._table_name,
                Key={"filing_key": _string(filing_key)},
                ConsistentRead=True,
                ProjectionExpression=(
                    "#status, #claim_owner, #attempt_count, #retryable"
                ),
                ExpressionAttributeNames={
                    "#status": "status",
                    "#claim_owner": "claim_owner",
                    "#attempt_count": "attempt_count",
                    "#retryable": "retryable",
                },
            )
        except Exception as error:
            _raise_storage_error(error, operation="registry item read")

        return _parse_optional_item(response.get("Item"), field="Item")

    def mark_raw_stored(self, request: MarkRawStoredRequest) -> None:
        """Persist raw-object metadata while conditionally releasing a claim."""
        names = {
            "#status": "status",
            "#claim_owner": "claim_owner",
            "#lease_expires_at_epoch": "lease_expires_at_epoch",
            "#raw_bucket": "raw_bucket",
            "#raw_key": "raw_key",
            "#raw_sha256": "raw_sha256",
            "#raw_content_length": "raw_content_length",
            "#raw_content_type": "raw_content_type",
            "#raw_stored_at": "raw_stored_at",
            "#updated_at": "updated_at",
            "#last_error_code": "last_error_code",
            "#last_error_message": "last_error_message",
            "#failed_at": "failed_at",
            "#retryable": "retryable",
        }
        values: dict[str, AttributeValue] = {
            ":fetching": _string("FETCHING"),
            ":raw_stored": _string("RAW_STORED"),
            ":owner": _string(request.owner_id),
            ":raw_bucket": _string(request.document.bucket),
            ":raw_key": _string(request.document.key),
            ":raw_sha256": _string(request.document.sha256.lower()),
            ":raw_content_length": _number_value(request.document.content_length),
            ":raw_content_type": _string(request.document.content_type),
            ":stored_at": _string(_timestamp(request.stored_at)),
        }
        self._owned_update(
            filing_key=request.filing_key,
            update_expression=(
                "SET #status = :raw_stored, #raw_bucket = :raw_bucket, "
                "#raw_key = :raw_key, #raw_sha256 = :raw_sha256, "
                "#raw_content_length = :raw_content_length, "
                "#raw_content_type = :raw_content_type, "
                "#raw_stored_at = :stored_at, #updated_at = :stored_at "
                "REMOVE #claim_owner, #lease_expires_at_epoch, "
                "#last_error_code, #last_error_message, #failed_at, #retryable"
            ),
            names=names,
            values=values,
        )

    def mark_failed(self, request: MarkFailedRequest) -> None:
        """Persist bounded failure details while conditionally releasing a claim."""
        names = {
            "#status": "status",
            "#claim_owner": "claim_owner",
            "#lease_expires_at_epoch": "lease_expires_at_epoch",
            "#last_error_code": "last_error_code",
            "#last_error_message": "last_error_message",
            "#failed_at": "failed_at",
            "#retryable": "retryable",
            "#updated_at": "updated_at",
        }
        failed_at = _string(_timestamp(request.failed_at))
        values: dict[str, AttributeValue] = {
            ":fetching": _string("FETCHING"),
            ":failed": _string("FAILED"),
            ":owner": _string(request.owner_id),
            ":error_code": _string(request.failure.code),
            ":error_message": _string(request.failure.message),
            ":failed_at": failed_at,
            ":retryable": {"BOOL": request.failure.retryable},
        }
        self._owned_update(
            filing_key=request.filing_key,
            update_expression=(
                "SET #status = :failed, #last_error_code = :error_code, "
                "#last_error_message = :error_message, #failed_at = :failed_at, "
                "#retryable = :retryable, #updated_at = :failed_at "
                "REMOVE #claim_owner, #lease_expires_at_epoch"
            ),
            names=names,
            values=values,
        )

    def _owned_update(
        self,
        *,
        filing_key: str,
        update_expression: str,
        names: dict[str, str],
        values: dict[str, AttributeValue],
    ) -> None:
        try:
            self._api.update_item(
                TableName=self._table_name,
                Key={"filing_key": _string(filing_key)},
                UpdateExpression=update_expression,
                ConditionExpression=("#status = :fetching AND #claim_owner = :owner"),
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ReturnValues="NONE",
            )
        except Exception as error:
            _raise_storage_error(error, operation="registry state update")


def _raise_storage_error(error: Exception, *, operation: str) -> NoReturn:
    if _is_conditional_check_failure(error):
        raise ConditionalWriteFailed(f"conditional {operation} failed") from error
    raise DynamoDbStorageError(f"DynamoDB {operation} failed") from error


def _claim_update_expression() -> str:
    metadata_fields = (
        "provider",
        "provider_filing_id",
        "provider_issuer_id",
        "issuer_name",
        "form",
        "filed_on",
        "report_date",
        "accepted_at",
        "primary_document",
        "filing_detail_url",
        "primary_document_url",
        "schema_version",
    )
    assignments = [f"#{field} = :{field}" for field in metadata_fields]
    assignments.extend(
        (
            "#status = :fetching",
            "#claim_owner = :owner",
            "#lease_expires_at_epoch = :lease_expires_at_epoch",
            "#first_discovered_at = if_not_exists(#first_discovered_at, :now)",
            "#last_claimed_at = :now",
            "#updated_at = :now",
            "#attempt_count = if_not_exists(#attempt_count, :zero) + :one",
        )
    )
    return (
        f"SET {', '.join(assignments)} "
        "REMOVE #last_error_code, #last_error_message, #failed_at, #retryable"
    )


def _claim_attribute_names() -> dict[str, str]:
    fields = (
        "filing_key",
        "provider",
        "provider_filing_id",
        "provider_issuer_id",
        "issuer_name",
        "form",
        "filed_on",
        "report_date",
        "accepted_at",
        "primary_document",
        "filing_detail_url",
        "primary_document_url",
        "schema_version",
        "status",
        "claim_owner",
        "lease_expires_at_epoch",
        "first_discovered_at",
        "last_claimed_at",
        "updated_at",
        "attempt_count",
        "last_error_code",
        "last_error_message",
        "failed_at",
        "retryable",
    )
    return {f"#{field}": field for field in fields}


def _claim_attribute_values(request: ClaimRequest) -> dict[str, AttributeValue]:
    filing = request.filing
    claimed_at = _timestamp(request.claimed_at)
    return {
        ":provider": _string(filing.provider),
        ":provider_filing_id": _string(filing.provider_filing_id),
        ":provider_issuer_id": _string(filing.issuer.provider_issuer_id),
        ":issuer_name": _string(filing.issuer_name),
        ":form": _string(filing.form.value),
        ":filed_on": _string(filing.filed_on.isoformat()),
        ":report_date": _optional_string(
            filing.report_date.isoformat() if filing.report_date is not None else None
        ),
        ":accepted_at": _optional_string(
            _timestamp(filing.accepted_at) if filing.accepted_at is not None else None
        ),
        ":primary_document": _string(filing.primary_document),
        ":filing_detail_url": _string(filing.filing_detail_url),
        ":primary_document_url": _string(filing.primary_document_url),
        ":schema_version": _number_value(SCHEMA_VERSION),
        ":fetching": _string("FETCHING"),
        ":failed": _string("FAILED"),
        ":true": {"BOOL": True},
        ":owner": _string(request.owner_id),
        ":lease_expires_at_epoch": _number_value(
            int(request.lease_expires_at.timestamp())
        ),
        ":now_epoch": _number_value(int(request.claimed_at.timestamp())),
        ":now": _string(claimed_at),
        ":zero": _number_value(0),
        ":one": _number_value(1),
    }


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _string(value: str) -> AttributeValue:
    return {"S": value}


def _optional_string(value: str | None) -> AttributeValue:
    return {"NULL": True} if value is None else _string(value)


def _number_value(value: int) -> AttributeValue:
    return {"N": str(value)}


def _parse_optional_item(value: object, *, field: str) -> StoredRegistryItem | None:
    if value is None or value == {}:
        return None
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) and isinstance(attribute, Mapping)
        for key, attribute in value.items()
    ):
        raise InvalidDynamoDbItemError(f"{field} must be a DynamoDB item")
    return StoredRegistryItem(
        status=_text(value, "status"),
        attempt_count=_number(value, "attempt_count"),
        owner_id=_optional_text(value, "claim_owner"),
        retryable=_optional_boolean(value, "retryable"),
    )


def _text(item: DynamoItem, field: str) -> str:
    attribute = item.get(field)
    if not isinstance(attribute, Mapping):
        raise InvalidDynamoDbItemError(f"DynamoDB item is missing {field!r}")
    value = attribute.get("S")
    if not isinstance(value, str) or not value:
        raise InvalidDynamoDbItemError(f"DynamoDB attribute {field!r} must be a string")
    return value


def _optional_text(item: DynamoItem, field: str) -> str | None:
    if field not in item:
        return None
    return _text(item, field)


def _number(item: DynamoItem, field: str) -> int:
    attribute = item.get(field)
    if not isinstance(attribute, Mapping):
        raise InvalidDynamoDbItemError(f"DynamoDB item is missing {field!r}")
    value = attribute.get("N")
    if not isinstance(value, str):
        raise InvalidDynamoDbItemError(f"DynamoDB attribute {field!r} must be a number")
    try:
        return int(value)
    except ValueError as error:
        raise InvalidDynamoDbItemError(
            f"DynamoDB attribute {field!r} must be an integer"
        ) from error


def _optional_boolean(item: DynamoItem, field: str) -> bool | None:
    if field not in item:
        return None
    attribute = item[field]
    if not isinstance(attribute, Mapping):
        raise InvalidDynamoDbItemError(f"DynamoDB attribute {field!r} must be boolean")
    value = attribute.get("BOOL")
    if not isinstance(value, bool):
        raise InvalidDynamoDbItemError(f"DynamoDB attribute {field!r} must be boolean")
    return value


def _is_conditional_check_failure(error: Exception) -> bool:
    response = getattr(error, "response", None)
    if not isinstance(response, Mapping):
        return False
    details = response.get("Error")
    return isinstance(details, Mapping) and (
        details.get("Code") == "ConditionalCheckFailedException"
    )
