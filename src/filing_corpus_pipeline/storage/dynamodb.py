"""DynamoDB client for filing registry persistence."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import NoReturn, Protocol

from filing_corpus_pipeline.domain import SourceDocumentReference
from filing_corpus_pipeline.registry.models import (
    ClaimRequest,
    MarkFailedRequest,
    MarkNormalizationFailedRequest,
    MarkNormalizedRequest,
    MarkRawStoredRequest,
    NormalizationClaimRequest,
    NormalizedCorpusMetadata,
    RawDocumentMetadata,
)

SCHEMA_VERSION = 3

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


@dataclass(frozen=True, slots=True)
class StoredNormalizationItem:
    """Registry fields required to coordinate normalization."""

    status: str
    attempt_count: int
    raw_document: RawDocumentMetadata
    parser_version: str | None
    owner_id: str | None
    retryable: bool | None
    corpus: NormalizedCorpusMetadata | None


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
            "#raw_source_document_name": "raw_source_document_name",
            "#raw_source_document_type": "raw_source_document_type",
            "#raw_source_document_description": "raw_source_document_description",
            "#raw_source_url": "raw_source_url",
            "#raw_resolver_version": "raw_resolver_version",
            "#raw_version_id": "raw_version_id",
            "#raw_etag": "raw_etag",
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
            ":raw_source_document_name": _string(
                request.document.source_document.document_name
            ),
            ":raw_source_document_type": _string(
                request.document.source_document.provider_document_type
            ),
            ":raw_source_document_description": _optional_string(
                request.document.source_document.description
            ),
            ":raw_source_url": _string(request.document.source_document.source_url),
            ":raw_resolver_version": _string(
                request.document.source_document.resolver_version
            ),
            ":raw_version_id": _optional_string(request.document.version_id),
            ":raw_etag": _optional_string(request.document.etag),
            ":stored_at": _string(_timestamp(request.stored_at)),
        }
        self._owned_update(
            filing_key=request.filing_key,
            update_expression=(
                "SET #status = :raw_stored, #raw_bucket = :raw_bucket, "
                "#raw_key = :raw_key, #raw_sha256 = :raw_sha256, "
                "#raw_content_length = :raw_content_length, "
                "#raw_content_type = :raw_content_type, "
                "#raw_source_document_name = :raw_source_document_name, "
                "#raw_source_document_type = :raw_source_document_type, "
                "#raw_source_document_description = "
                ":raw_source_document_description, "
                "#raw_source_url = :raw_source_url, "
                "#raw_resolver_version = :raw_resolver_version, "
                "#raw_version_id = :raw_version_id, #raw_etag = :raw_etag, "
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

    def claim_normalization(
        self,
        request: NormalizationClaimRequest,
    ) -> StoredNormalizationItem:
        """Atomically acquire normalization and return the previous raw state."""
        names = _normalization_attribute_names(
            "status",
            "updated_at",
            "normalization_attempt_count",
            "normalization_owner",
            "normalization_lease_expires_at_epoch",
            "normalization_parser_version",
            "normalization_last_claimed_at",
            "normalization_error_code",
            "normalization_error_message",
            "normalization_failed_at",
            "normalization_retryable",
        )
        values = {
            ":raw_stored": _string("RAW_STORED"),
            ":normalizing": _string("NORMALIZING"),
            ":normalized": _string("NORMALIZED"),
            ":normalization_failed": _string("NORMALIZATION_FAILED"),
            ":true": {"BOOL": True},
            ":owner": _string(request.owner_id),
            ":parser_version": _string(request.parser_version),
            ":lease_expires_at_epoch": _number_value(
                int(request.lease_expires_at.timestamp())
            ),
            ":now_epoch": _number_value(int(request.claimed_at.timestamp())),
            ":now": _string(_timestamp(request.claimed_at)),
            ":zero": _number_value(0),
            ":one": _number_value(1),
        }
        try:
            response = self._api.update_item(
                TableName=self._table_name,
                Key={"filing_key": _string(request.filing_key)},
                UpdateExpression=(
                    "SET #status = :normalizing, #normalization_owner = :owner, "
                    "#normalization_lease_expires_at_epoch = :lease_expires_at_epoch, "
                    "#normalization_parser_version = :parser_version, "
                    "#normalization_last_claimed_at = :now, #updated_at = :now, "
                    "#normalization_attempt_count = "
                    "if_not_exists(#normalization_attempt_count, :zero) + :one "
                    "REMOVE #normalization_error_code, #normalization_error_message, "
                    "#normalization_failed_at, #normalization_retryable"
                ),
                ConditionExpression=(
                    "#status = :raw_stored OR "
                    "(#status = :normalization_failed AND "
                    "(#normalization_retryable = :true OR "
                    "attribute_not_exists(#normalization_parser_version) OR "
                    "#normalization_parser_version <> :parser_version)) OR "
                    "(#status = :normalizing AND "
                    "(#normalization_owner = :owner OR "
                    "attribute_not_exists(#normalization_lease_expires_at_epoch) OR "
                    "#normalization_lease_expires_at_epoch <= :now_epoch)) OR "
                    "(#status = :normalized AND "
                    "(attribute_not_exists(#normalization_parser_version) OR "
                    "#normalization_parser_version <> :parser_version))"
                ),
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ReturnValues="ALL_OLD",
            )
        except Exception as error:
            _raise_storage_error(error, operation="normalization claim")

        previous = _parse_normalization_item(response.get("Attributes"))
        if previous is None:
            raise InvalidDynamoDbItemError(
                "normalization claim succeeded without a prior raw registry item"
            )
        return previous

    def get_normalization(self, filing_key: str) -> StoredNormalizationItem | None:
        """Read the current state needed to classify a normalization conflict."""
        fields = (
            "status",
            "normalization_attempt_count",
            "normalization_owner",
            "normalization_retryable",
            "normalization_parser_version",
            *_raw_fields(),
            *_legacy_raw_source_fields(),
            *_corpus_fields(),
        )
        names = _normalization_attribute_names(*fields)
        projection = ", ".join(f"#{field}" for field in fields)
        try:
            response = self._api.get_item(
                TableName=self._table_name,
                Key={"filing_key": _string(filing_key)},
                ConsistentRead=True,
                ProjectionExpression=projection,
                ExpressionAttributeNames=names,
            )
        except Exception as error:
            _raise_storage_error(error, operation="normalization item read")
        return _parse_normalization_item(response.get("Item"))

    def mark_normalized(self, request: MarkNormalizedRequest) -> None:
        """Publish normalized corpus metadata and release its claim."""
        corpus = request.corpus
        names = _normalization_attribute_names(
            "status",
            "updated_at",
            "normalization_owner",
            "normalization_lease_expires_at_epoch",
            "normalization_parser_version",
            "normalization_error_code",
            "normalization_error_message",
            "normalization_failed_at",
            "normalization_retryable",
            "normalized_at",
            *_corpus_fields(),
        )
        values: dict[str, AttributeValue] = {
            ":normalizing": _string("NORMALIZING"),
            ":normalized": _string("NORMALIZED"),
            ":owner": _string(request.owner_id),
            ":normalized_at": _string(_timestamp(request.normalized_at)),
            ":normalized_bucket": _string(corpus.bucket),
            ":normalized_prefix": _string(corpus.prefix),
            ":normalized_manifest_key": _string(corpus.manifest_key),
            ":normalized_manifest_sha256": _string(corpus.manifest_sha256.lower()),
            ":normalized_blocks_key": _string(corpus.blocks_key),
            ":normalized_blocks_sha256": _string(corpus.blocks_sha256.lower()),
            ":normalization_parser_version": _string(corpus.parser_version),
            ":normalization_schema_version": _string(corpus.schema_version),
            ":normalized_block_count": _number_value(corpus.block_count),
            ":normalized_section_count": _number_value(corpus.section_count),
            ":normalized_warning_count": _number_value(corpus.warning_count),
            ":normalized_quality_status": _string(corpus.quality_status),
        }
        self._owned_normalization_update(
            filing_key=request.filing_key,
            update_expression=(
                "SET #status = :normalized, #normalized_bucket = :normalized_bucket, "
                "#normalized_prefix = :normalized_prefix, "
                "#normalized_manifest_key = :normalized_manifest_key, "
                "#normalized_manifest_sha256 = :normalized_manifest_sha256, "
                "#normalized_blocks_key = :normalized_blocks_key, "
                "#normalized_blocks_sha256 = :normalized_blocks_sha256, "
                "#normalization_parser_version = :normalization_parser_version, "
                "#normalization_schema_version = :normalization_schema_version, "
                "#normalized_block_count = :normalized_block_count, "
                "#normalized_section_count = :normalized_section_count, "
                "#normalized_warning_count = :normalized_warning_count, "
                "#normalized_quality_status = :normalized_quality_status, "
                "#normalized_at = :normalized_at, #updated_at = :normalized_at "
                "REMOVE #normalization_owner, "
                "#normalization_lease_expires_at_epoch, #normalization_error_code, "
                "#normalization_error_message, #normalization_failed_at, "
                "#normalization_retryable"
            ),
            names=names,
            values=values,
        )

    def mark_normalization_failed(
        self,
        request: MarkNormalizationFailedRequest,
    ) -> None:
        """Persist normalization failure details and release its claim."""
        names = _normalization_attribute_names(
            "status",
            "updated_at",
            "normalization_owner",
            "normalization_lease_expires_at_epoch",
            "normalization_parser_version",
            "normalization_error_code",
            "normalization_error_message",
            "normalization_failed_at",
            "normalization_retryable",
        )
        values: dict[str, AttributeValue] = {
            ":normalizing": _string("NORMALIZING"),
            ":normalization_failed": _string("NORMALIZATION_FAILED"),
            ":owner": _string(request.owner_id),
            ":parser_version": _string(request.parser_version),
            ":error_code": _string(request.failure.code),
            ":error_message": _string(request.failure.message),
            ":failed_at": _string(_timestamp(request.failed_at)),
            ":retryable": {"BOOL": request.failure.retryable},
        }
        self._owned_normalization_update(
            filing_key=request.filing_key,
            update_expression=(
                "SET #status = :normalization_failed, "
                "#normalization_parser_version = :parser_version, "
                "#normalization_error_code = :error_code, "
                "#normalization_error_message = :error_message, "
                "#normalization_failed_at = :failed_at, "
                "#normalization_retryable = :retryable, #updated_at = :failed_at "
                "REMOVE #normalization_owner, #normalization_lease_expires_at_epoch"
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

    def _owned_normalization_update(
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
                ConditionExpression=(
                    "#status = :normalizing AND #normalization_owner = :owner"
                ),
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ReturnValues="NONE",
            )
        except Exception as error:
            _raise_storage_error(error, operation="normalization state update")


def _raise_storage_error(error: Exception, *, operation: str) -> NoReturn:
    if _is_conditional_check_failure(error):
        raise ConditionalWriteFailed(f"conditional {operation} failed") from error
    raise DynamoDbStorageError(f"DynamoDB {operation} failed") from error


def _claim_update_expression() -> str:
    metadata_fields = (
        "company_id",
        "provider",
        "provider_filing_id",
        "provider_issuer_id",
        "issuer_name",
        "filing_type",
        "document_policy",
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
        "company_id",
        "provider",
        "provider_filing_id",
        "provider_issuer_id",
        "issuer_name",
        "filing_type",
        "document_policy",
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
        ":company_id": _string(filing.company_id),
        ":provider": _string(filing.provider),
        ":provider_filing_id": _string(filing.provider_filing_id),
        ":provider_issuer_id": _string(filing.issuer.provider_issuer_id),
        ":issuer_name": _string(filing.issuer_name),
        ":filing_type": _string(filing.filing_type),
        ":document_policy": _string(filing.document_policy.value),
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


def _raw_fields() -> tuple[str, ...]:
    return (
        "raw_bucket",
        "raw_key",
        "raw_sha256",
        "raw_content_length",
        "raw_content_type",
        "raw_source_document_name",
        "raw_source_document_type",
        "raw_source_document_description",
        "raw_source_url",
        "raw_resolver_version",
        "raw_version_id",
        "raw_etag",
    )


def _legacy_raw_source_fields() -> tuple[str, ...]:
    return (
        "primary_document",
        "filing_type",
        "primary_document_url",
    )


def _corpus_fields() -> tuple[str, ...]:
    return (
        "normalized_bucket",
        "normalized_prefix",
        "normalized_manifest_key",
        "normalized_manifest_sha256",
        "normalized_blocks_key",
        "normalized_blocks_sha256",
        "normalization_schema_version",
        "normalized_block_count",
        "normalized_section_count",
        "normalized_warning_count",
        "normalized_quality_status",
    )


def _normalization_attribute_names(*fields: str) -> dict[str, str]:
    return {f"#{field}": field for field in fields}


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


def _parse_normalization_item(value: object) -> StoredNormalizationItem | None:
    if value is None or value == {}:
        return None
    item = _validated_item(value, field="normalization registry item")
    try:
        raw_document = RawDocumentMetadata(
            source_document=_parse_raw_source_document(item),
            bucket=_text(item, "raw_bucket"),
            key=_text(item, "raw_key"),
            sha256=_text(item, "raw_sha256"),
            content_length=_number(item, "raw_content_length"),
            content_type=_text(item, "raw_content_type"),
            version_id=_nullable_text(item, "raw_version_id"),
            etag=_nullable_text(item, "raw_etag"),
        )
    except ValueError as error:
        raise InvalidDynamoDbItemError(
            f"invalid raw document metadata: {error}"
        ) from error
    status = _text(item, "status")
    parser_version = _optional_text(item, "normalization_parser_version")
    corpus = (
        _parse_optional_corpus(item, parser_version=parser_version)
        if status == "NORMALIZED"
        else None
    )
    return StoredNormalizationItem(
        status=status,
        attempt_count=_optional_number(item, "normalization_attempt_count") or 0,
        raw_document=raw_document,
        parser_version=parser_version,
        owner_id=_optional_text(item, "normalization_owner"),
        retryable=_optional_boolean(item, "normalization_retryable"),
        corpus=corpus,
    )


def _parse_raw_source_document(item: DynamoItem) -> SourceDocumentReference:
    source_fields = (
        "raw_source_document_name",
        "raw_source_document_type",
        "raw_source_document_description",
        "raw_source_url",
        "raw_resolver_version",
    )
    if any(field in item for field in source_fields):
        return SourceDocumentReference(
            document_name=_text(item, "raw_source_document_name"),
            provider_document_type=_text(item, "raw_source_document_type"),
            description=_nullable_text(item, "raw_source_document_description"),
            source_url=_text(item, "raw_source_url"),
            resolver_version=_text(item, "raw_resolver_version"),
        )
    return SourceDocumentReference(
        document_name=_text(item, "primary_document"),
        provider_document_type=_text(item, "filing_type"),
        description=None,
        source_url=_text(item, "primary_document_url"),
        resolver_version="legacy-primary-v1",
    )


def _parse_optional_corpus(
    item: DynamoItem,
    *,
    parser_version: str | None,
) -> NormalizedCorpusMetadata | None:
    if "normalized_bucket" not in item:
        return None
    if parser_version is None:
        raise InvalidDynamoDbItemError(
            "normalized corpus is missing 'normalization_parser_version'"
        )
    try:
        return NormalizedCorpusMetadata(
            bucket=_text(item, "normalized_bucket"),
            prefix=_text(item, "normalized_prefix"),
            manifest_key=_text(item, "normalized_manifest_key"),
            manifest_sha256=_text(item, "normalized_manifest_sha256"),
            blocks_key=_text(item, "normalized_blocks_key"),
            blocks_sha256=_text(item, "normalized_blocks_sha256"),
            parser_version=parser_version,
            schema_version=_text(item, "normalization_schema_version"),
            block_count=_number(item, "normalized_block_count"),
            section_count=_number(item, "normalized_section_count"),
            warning_count=_number(item, "normalized_warning_count"),
            quality_status=_text(item, "normalized_quality_status"),
        )
    except ValueError as error:
        raise InvalidDynamoDbItemError(
            f"invalid normalized corpus metadata: {error}"
        ) from error


def _validated_item(value: object, *, field: str) -> DynamoItem:
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) and isinstance(attribute, Mapping)
        for key, attribute in value.items()
    ):
        raise InvalidDynamoDbItemError(f"{field} must be a DynamoDB item")
    return value


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


def _nullable_text(item: DynamoItem, field: str) -> str | None:
    if field not in item:
        return None
    attribute = item[field]
    if isinstance(attribute, Mapping) and attribute.get("NULL") is True:
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


def _optional_number(item: DynamoItem, field: str) -> int | None:
    if field not in item:
        return None
    return _number(item, field)


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
