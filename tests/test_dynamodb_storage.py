"""Tests for DynamoDB serialization and conditional storage operations."""

from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from typing import ClassVar

import pytest

from filing_corpus_pipeline.domain import FilingForm, FilingReference, IssuerReference
from filing_corpus_pipeline.registry import (
    ClaimRequest,
    FailureDetails,
    MarkFailedRequest,
    MarkNormalizationFailedRequest,
    MarkNormalizedRequest,
    MarkRawStoredRequest,
    NormalizationClaimRequest,
    NormalizedCorpusMetadata,
    RawDocumentMetadata,
)
from filing_corpus_pipeline.storage import (
    ConditionalWriteFailed,
    DynamoDbRegistryClient,
    DynamoDbStorageError,
    InvalidDynamoDbItemError,
)


class ConditionalCheckFailed(Exception):
    """Minimal stand-in for botocore's conditional failure."""

    response: ClassVar[dict[str, object]] = {
        "Error": {"Code": "ConditionalCheckFailedException"}
    }


class StubDynamoDbApi:
    """Scripted low-level API that records DynamoDB requests."""

    def __init__(
        self,
        *,
        updates: list[Mapping[str, object] | Exception] | None = None,
        reads: list[Mapping[str, object] | Exception] | None = None,
    ) -> None:
        self.updates = list(updates or [{}])
        self.reads = list(reads or [])
        self.update_calls: list[dict[str, object]] = []
        self.get_calls: list[dict[str, object]] = []

    def update_item(self, **kwargs: object) -> Mapping[str, object]:
        self.update_calls.append(kwargs)
        result = self.updates.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def get_item(self, **kwargs: object) -> Mapping[str, object]:
        self.get_calls.append(kwargs)
        result = self.reads.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def filing_reference() -> FilingReference:
    """Build the filing passed from discovery to acquisition."""
    return FilingReference(
        provider="sec",
        provider_filing_id="0000320193-25-000079",
        issuer=IssuerReference("sec", "0000320193"),
        issuer_name="Apple Inc.",
        form=FilingForm.TEN_Q,
        filed_on=date(2025, 8, 1),
        report_date=None,
        accepted_at=None,
        primary_document="aapl-20250628.htm",
        filing_detail_url="https://example.test/filing-index.html",
        primary_document_url="https://example.test/aapl-20250628.htm",
    )


def claim_request() -> ClaimRequest:
    """Build a deterministic five-minute claim."""
    return ClaimRequest(
        filing=filing_reference(),
        owner_id="execution-1",
        claimed_at=datetime(2025, 8, 1, 18, 0, tzinfo=UTC),
        lease_duration=timedelta(minutes=5),
    )


def storage(api: StubDynamoDbApi) -> DynamoDbRegistryClient:
    """Create the storage client against the test table name."""
    return DynamoDbRegistryClient(api, table_name="filing-registry")


def test_claim_serializes_an_atomic_conditional_update() -> None:
    """The storage client emits one race-safe write for a new claim."""
    api = StubDynamoDbApi()

    previous = storage(api).claim(claim_request())

    assert previous is None
    call = api.update_calls[0]
    assert call["Key"] == {"filing_key": {"S": "sec#0000320193-25-000079"}}
    assert "attribute_not_exists(#filing_key)" in str(call["ConditionExpression"])
    assert "#status = :failed AND #retryable = :true" in str(
        call["ConditionExpression"]
    )
    values = call["ExpressionAttributeValues"]
    assert isinstance(values, dict)
    assert values[":lease_expires_at_epoch"] == {"N": "1754071500"}
    assert values[":report_date"] == {"NULL": True}
    assert values[":schema_version"] == {"N": "1"}


def test_claim_deserializes_the_previous_registry_item() -> None:
    """Prior state lets the service distinguish a reclaim from a first claim."""
    api = StubDynamoDbApi(
        updates=[
            {
                "Attributes": {
                    "status": {"S": "FAILED"},
                    "attempt_count": {"N": "2"},
                    "retryable": {"BOOL": True},
                }
            }
        ]
    )

    previous = storage(api).claim(claim_request())

    assert previous is not None
    assert previous.status == "FAILED"
    assert previous.attempt_count == 2
    assert previous.retryable is True
    assert previous.owner_id is None


def test_get_uses_a_consistent_projected_read() -> None:
    """Conflict classification reads only the required current fields."""
    api = StubDynamoDbApi(
        reads=[
            {
                "Item": {
                    "status": {"S": "FETCHING"},
                    "attempt_count": {"N": "1"},
                    "claim_owner": {"S": "execution-other"},
                }
            }
        ]
    )

    current = storage(api).get("sec#filing")

    assert current is not None
    assert current.owner_id == "execution-other"
    assert current.retryable is None
    assert api.get_calls[0]["ConsistentRead"] is True
    assert api.get_calls[0]["ProjectionExpression"] == (
        "#status, #claim_owner, #attempt_count, #retryable"
    )


@pytest.mark.parametrize("operation", ["claim", "stored"])
def test_conditional_failures_are_translated_for_the_service(operation: str) -> None:
    """The feature service does not inspect botocore error payloads."""
    api = StubDynamoDbApi(updates=[ConditionalCheckFailed()])
    client = storage(api)

    with pytest.raises(ConditionalWriteFailed):
        if operation == "claim":
            client.claim(claim_request())
        else:
            client.mark_raw_stored(raw_stored_request())


def test_nonconditional_sdk_failures_are_storage_errors() -> None:
    """Provider failures remain distinct from conditional conflicts."""
    api = StubDynamoDbApi(updates=[RuntimeError("offline")])

    with pytest.raises(DynamoDbStorageError, match="filing claim"):
        storage(api).claim(claim_request())


def raw_stored_request() -> MarkRawStoredRequest:
    """Build a completed raw-document transition."""
    return MarkRawStoredRequest(
        filing_key="sec#0000320193-25-000079",
        owner_id="execution-1",
        stored_at=datetime(2025, 8, 1, 18, 1, tzinfo=UTC),
        document=RawDocumentMetadata(
            bucket="filing-corpus-raw",
            key="raw/sec/0000320193/filing.htm",
            sha256="A" * 64,
            content_length=1024,
            content_type="text/html",
            version_id="version-1",
            etag="etag-1",
        ),
    )


def test_mark_raw_stored_serializes_integrity_metadata() -> None:
    """Raw object evidence is written only under the active owner condition."""
    api = StubDynamoDbApi()

    storage(api).mark_raw_stored(raw_stored_request())

    call = api.update_calls[0]
    assert call["ConditionExpression"] == (
        "#status = :fetching AND #claim_owner = :owner"
    )
    values = call["ExpressionAttributeValues"]
    assert isinstance(values, dict)
    assert values[":raw_sha256"] == {"S": "a" * 64}
    assert values[":raw_content_length"] == {"N": "1024"}
    assert values[":raw_version_id"] == {"S": "version-1"}
    assert values[":raw_etag"] == {"S": "etag-1"}
    assert "REMOVE #claim_owner, #lease_expires_at_epoch" in str(
        call["UpdateExpression"]
    )


def test_mark_failed_serializes_bounded_diagnostics() -> None:
    """Failed work is persisted in a recoverable, queryable shape."""
    api = StubDynamoDbApi()
    request = MarkFailedRequest(
        filing_key="sec#0000320193-25-000079",
        owner_id="execution-1",
        failed_at=datetime(2025, 8, 1, 18, 1, tzinfo=UTC),
        failure=FailureDetails(
            code="SEC_HTTP_ERROR",
            message="SEC returned HTTP 503",
            retryable=True,
        ),
    )

    storage(api).mark_failed(request)

    values = api.update_calls[0]["ExpressionAttributeValues"]
    assert isinstance(values, dict)
    assert values[":failed"] == {"S": "FAILED"}
    assert values[":retryable"] == {"BOOL": True}
    assert values[":error_code"] == {"S": "SEC_HTTP_ERROR"}


@pytest.mark.parametrize(
    "stored_item",
    [
        "not-an-item",
        {"attempt_count": {"N": "1"}},
        {"status": {"S": "FETCHING"}, "attempt_count": {"N": "not-int"}},
        {
            "status": {"S": "FAILED"},
            "attempt_count": {"N": "1"},
            "retryable": {"S": "yes"},
        },
    ],
)
def test_get_rejects_malformed_dynamodb_items(stored_item: object) -> None:
    """DynamoDB shape drift is rejected at the storage boundary."""
    api = StubDynamoDbApi(reads=[{"Item": stored_item}])

    with pytest.raises(InvalidDynamoDbItemError):
        storage(api).get("sec#filing")


def test_storage_requires_a_table_name() -> None:
    """Misconfigured runtime composition fails at startup."""
    with pytest.raises(ValueError, match="table_name"):
        DynamoDbRegistryClient(StubDynamoDbApi(), table_name="")


def normalization_claim_request() -> NormalizationClaimRequest:
    return NormalizationClaimRequest(
        filing_key="sec#0000320193-25-000079",
        owner_id="execution-1",
        claimed_at=datetime(2025, 8, 1, 18, 0, tzinfo=UTC),
        lease_duration=timedelta(minutes=10),
        parser_version="sec-html-v2",
    )


def raw_attributes() -> dict[str, object]:
    return {
        "status": {"S": "RAW_STORED"},
        "raw_bucket": {"S": "raw-bucket"},
        "raw_key": {"S": "raw/sec/filing.htm"},
        "raw_sha256": {"S": "a" * 64},
        "raw_content_length": {"N": "1024"},
        "raw_content_type": {"S": "text/html"},
        "raw_version_id": {"S": "version-1"},
        "raw_etag": {"NULL": True},
    }


def corpus_metadata() -> NormalizedCorpusMetadata:
    return NormalizedCorpusMetadata(
        bucket="normalized-bucket",
        prefix="normalized/prefix",
        manifest_key="normalized/prefix/manifest.json",
        manifest_sha256="b" * 64,
        blocks_key="normalized/prefix/blocks.jsonl.gz",
        blocks_sha256="c" * 64,
        parser_version="sec-html-v2",
        schema_version="1",
        block_count=20,
        section_count=3,
        warning_count=1,
        quality_status="WARN",
    )


def test_claim_normalization_serializes_lease_and_returns_raw_metadata() -> None:
    api = StubDynamoDbApi(updates=[{"Attributes": raw_attributes()}])

    previous = storage(api).claim_normalization(normalization_claim_request())

    assert previous.status == "RAW_STORED"
    assert previous.attempt_count == 0
    assert previous.raw_document.version_id == "version-1"
    call = api.update_calls[0]
    assert "#status = :raw_stored" in str(call["ConditionExpression"])
    assert "#normalization_parser_version <> :parser_version" in str(
        call["ConditionExpression"]
    )
    values = call["ExpressionAttributeValues"]
    assert isinstance(values, dict)
    assert values[":lease_expires_at_epoch"] == {"N": "1754071800"}


def test_get_normalization_deserializes_committed_corpus_metadata() -> None:
    item = {
        **raw_attributes(),
        "status": {"S": "NORMALIZED"},
        "normalization_attempt_count": {"N": "2"},
        "normalization_parser_version": {"S": "sec-html-v2"},
        "normalized_bucket": {"S": "normalized-bucket"},
        "normalized_prefix": {"S": "normalized/prefix"},
        "normalized_manifest_key": {"S": "normalized/prefix/manifest.json"},
        "normalized_manifest_sha256": {"S": "b" * 64},
        "normalized_blocks_key": {"S": "normalized/prefix/blocks.jsonl.gz"},
        "normalized_blocks_sha256": {"S": "c" * 64},
        "normalization_schema_version": {"S": "1"},
        "normalized_block_count": {"N": "20"},
        "normalized_section_count": {"N": "3"},
        "normalized_warning_count": {"N": "1"},
        "normalized_quality_status": {"S": "WARN"},
    }
    api = StubDynamoDbApi(reads=[{"Item": item}])

    current = storage(api).get_normalization("sec#filing")

    assert current is not None
    assert current.corpus == corpus_metadata()
    assert current.attempt_count == 2
    assert api.get_calls[0]["ConsistentRead"] is True


def test_mark_normalized_serializes_corpus_and_ownership_condition() -> None:
    api = StubDynamoDbApi()
    request = MarkNormalizedRequest(
        filing_key="sec#filing",
        owner_id="execution-1",
        normalized_at=datetime(2025, 8, 1, 18, 2, tzinfo=UTC),
        corpus=corpus_metadata(),
    )

    storage(api).mark_normalized(request)

    call = api.update_calls[0]
    assert call["ConditionExpression"] == (
        "#status = :normalizing AND #normalization_owner = :owner"
    )
    values = call["ExpressionAttributeValues"]
    assert isinstance(values, dict)
    assert values[":normalized_block_count"] == {"N": "20"}
    assert values[":normalized_quality_status"] == {"S": "WARN"}


def test_mark_normalization_failed_uses_separate_diagnostics() -> None:
    api = StubDynamoDbApi()
    request = MarkNormalizationFailedRequest(
        filing_key="sec#filing",
        owner_id="execution-1",
        failed_at=datetime(2025, 8, 1, 18, 2, tzinfo=UTC),
        parser_version="sec-html-v2",
        failure=FailureDetails("NO_CONTENT", "no visible content", False),
    )

    storage(api).mark_normalization_failed(request)

    call = api.update_calls[0]
    assert "#normalization_error_code = :error_code" in str(call["UpdateExpression"])
    values = call["ExpressionAttributeValues"]
    assert isinstance(values, dict)
    assert values[":retryable"] == {"BOOL": False}
