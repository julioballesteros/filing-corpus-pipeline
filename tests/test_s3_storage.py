"""Tests for idempotent raw-document S3 storage."""

import base64
from collections.abc import Mapping
from hashlib import sha256
from typing import ClassVar, cast

import pytest

from filing_corpus_pipeline.domain import SourceDocumentReference
from filing_corpus_pipeline.registry import RawDocumentMetadata
from filing_corpus_pipeline.storage.normalized_corpus import (
    NormalizedCorpusWrite,
    NormalizedObjectCollisionError,
    NormalizedObjectStorageError,
    S3NormalizedCorpusClient,
)
from filing_corpus_pipeline.storage.raw_documents import (
    RawObjectCollisionError,
    RawObjectIntegrityError,
    RawObjectStorageError,
    RawObjectWrite,
    S3RawDocumentClient,
)


class AwsError(Exception):
    """Minimal botocore-compatible error payload."""

    def __init__(self, code: str, status_code: int) -> None:
        super().__init__(code)
        self.response = {
            "Error": {"Code": code},
            "ResponseMetadata": {"HTTPStatusCode": status_code},
        }


class PreconditionFailed(Exception):
    """An existing deterministic key rejected create-only storage."""

    response: ClassVar[dict[str, object]] = {
        "Error": {"Code": "PreconditionFailed"},
        "ResponseMetadata": {"HTTPStatusCode": 412},
    }


class StubS3Api:
    """Script S3 responses and record SDK request shapes."""

    def __init__(
        self,
        *,
        puts: list[Mapping[str, object] | Exception] | None = None,
        heads: list[Mapping[str, object] | Exception] | None = None,
        gets: list[Mapping[str, object] | Exception] | None = None,
    ) -> None:
        self.puts = list(puts or [{}])
        self.heads = list(heads or [])
        self.gets = list(gets or [])
        self.put_calls: list[dict[str, object]] = []
        self.head_calls: list[dict[str, object]] = []
        self.get_calls: list[dict[str, object]] = []

    def put_object(self, **kwargs: object) -> Mapping[str, object]:
        self.put_calls.append(kwargs)
        result = self.puts.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def head_object(self, **kwargs: object) -> Mapping[str, object]:
        self.head_calls.append(kwargs)
        result = self.heads.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def get_object(self, **kwargs: object) -> Mapping[str, object]:
        self.get_calls.append(kwargs)
        result = self.gets.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class BodyStream:
    """Bounded in-memory stand-in for botocore's streaming response body."""

    def __init__(self, body: bytes) -> None:
        self.body = body
        self.amounts: list[int | None] = []

    def read(self, amt: int | None = None) -> bytes:
        self.amounts.append(amt)
        return self.body if amt is None else self.body[:amt]


BODY = b"<html>filing</html>"
DIGEST = sha256(BODY).hexdigest()


def source_document() -> SourceDocumentReference:
    """Build the selected provider document stored as raw provenance."""
    return SourceDocumentReference(
        document_name="report.htm",
        provider_document_type="10-Q",
        description=None,
        source_url="https://www.sec.gov/report.htm",
        resolver_version="sec-primary-v1",
    )


def write_request() -> RawObjectWrite:
    """Build a complete object write with source provenance."""
    return RawObjectWrite(
        key="raw/sec/320193/accession/report.htm",
        body=BODY,
        sha256=DIGEST,
        content_type="text/html",
        filing_key="sec#accession",
        source_document=source_document(),
        source_etag='"source"',
        source_last_modified="Fri, 01 Aug 2025 18:00:00 GMT",
    )


def storage(api: StubS3Api) -> S3RawDocumentClient:
    """Build storage against the portfolio bucket."""
    return S3RawDocumentClient(api, bucket_name="filing-corpus-raw")


def test_store_creates_an_encrypted_checksum_verified_object_once() -> None:
    """The first write is create-only and contains integrity metadata."""
    api = StubS3Api(puts=[{"VersionId": "version-1", "ETag": '"etag-1"'}])

    result = storage(api).store(write_request())

    call = api.put_calls[0]
    assert call["IfNoneMatch"] == "*"
    assert call["ServerSideEncryption"] == "AES256"
    assert call["ChecksumAlgorithm"] == "SHA256"
    assert call["ChecksumSHA256"] == base64.b64encode(bytes.fromhex(DIGEST)).decode(
        "ascii"
    )
    metadata = call["Metadata"]
    assert isinstance(metadata, dict)
    assert metadata["sha256"] == DIGEST
    assert metadata["source-etag"] == '"source"'
    assert metadata["source-url"] == "https://www.sec.gov/report.htm"
    assert metadata["source-document-name"] == "report.htm"
    assert metadata["source-document-type"] == "10-Q"
    assert metadata["resolver-version"] == "sec-primary-v1"
    assert result.version_id == "version-1"
    assert result.etag == "etag-1"
    assert result.reused is False


def test_store_reuses_an_identical_existing_object() -> None:
    """A Lambda retry never creates another version for identical bytes."""
    api = StubS3Api(
        puts=[PreconditionFailed()],
        heads=[
            {
                "Metadata": {"sha256": DIGEST.upper()},
                "ContentLength": len(BODY),
                "VersionId": "version-1",
                "ETag": "etag-1",
            }
        ],
    )

    result = storage(api).store(write_request())

    assert result.reused is True
    assert result.version_id == "version-1"
    assert api.head_calls == [
        {
            "Bucket": "filing-corpus-raw",
            "Key": "raw/sec/320193/accession/report.htm",
        }
    ]


@pytest.mark.parametrize(
    "head",
    [
        {"Metadata": {}, "ContentLength": len(BODY)},
        {"Metadata": {"sha256": "0" * 64}, "ContentLength": len(BODY)},
        {"Metadata": {"sha256": DIGEST}, "ContentLength": len(BODY) + 1},
    ],
)
def test_store_rejects_a_same_key_with_different_integrity(
    head: Mapping[str, object],
) -> None:
    """Deterministic-key collisions are permanent data-integrity failures."""
    api = StubS3Api(puts=[PreconditionFailed()], heads=[head])

    with pytest.raises(RawObjectCollisionError) as raised:
        storage(api).store(write_request())

    assert raised.value.code == "RAW_OBJECT_COLLISION"
    assert raised.value.retryable is False


@pytest.mark.parametrize(
    ("error", "retryable", "code"),
    [
        (AwsError("SlowDown", 503), True, "S3_SLOWDOWN"),
        (AwsError("AccessDenied", 403), False, "S3_ACCESSDENIED"),
        (RuntimeError("offline"), True, "S3_SDK_ERROR"),
    ],
)
def test_store_classifies_sdk_write_errors(
    error: Exception,
    retryable: bool,
    code: str,
) -> None:
    """Workflow retries are limited to transient S3 failures."""
    with pytest.raises(RawObjectStorageError) as raised:
        storage(StubS3Api(puts=[error])).store(write_request())

    assert raised.value.retryable is retryable
    assert raised.value.code == code


def test_store_classifies_verification_errors() -> None:
    """A failed HEAD after a duplicate indication remains safely retryable."""
    api = StubS3Api(
        puts=[PreconditionFailed()],
        heads=[AwsError("InternalError", 500)],
    )

    with pytest.raises(RawObjectStorageError) as raised:
        storage(api).store(write_request())

    assert raised.value.retryable is True


@pytest.mark.parametrize(
    "overrides",
    [
        {"key": ""},
        {"body": b""},
        {"sha256": "bad"},
        {"sha256": "z" * 64},
        {"content_type": ""},
    ],
)
def test_raw_object_write_rejects_incomplete_integrity_values(
    overrides: dict[str, object],
) -> None:
    """Invalid object requests fail before invoking S3."""
    values: dict[str, object] = {
        "key": "key",
        "body": BODY,
        "sha256": DIGEST,
        "content_type": "text/html",
        "filing_key": "sec#filing",
        "source_document": source_document(),
    }
    values.update(overrides)

    with pytest.raises(ValueError):
        RawObjectWrite(**values)  # type: ignore[arg-type]


def test_storage_requires_a_bucket_name() -> None:
    """A missing runtime setting is caught during composition."""
    with pytest.raises(ValueError):
        S3RawDocumentClient(StubS3Api(), bucket_name="")


def raw_metadata(**overrides: object) -> RawDocumentMetadata:
    values: dict[str, object] = {
        "source_document": source_document(),
        "bucket": "filing-corpus-raw",
        "key": "raw/sec/320193/accession/report.htm",
        "sha256": DIGEST,
        "content_length": len(BODY),
        "content_type": "text/html",
        "version_id": "version-1",
    }
    values.update(overrides)
    return RawDocumentMetadata(**values)  # type: ignore[arg-type]


def test_load_reads_a_version_and_verifies_length_and_digest() -> None:
    stream = BodyStream(BODY)
    api = StubS3Api(gets=[{"ContentLength": len(BODY), "Body": stream}])

    loaded = storage(api).load(raw_metadata(), max_bytes=1024)

    assert loaded == BODY
    assert stream.amounts == [1025]
    assert api.get_calls[0]["VersionId"] == "version-1"


@pytest.mark.parametrize(
    ("metadata", "response", "code"),
    [
        (raw_metadata(bucket="other"), None, "RAW_BUCKET_MISMATCH"),
        (raw_metadata(content_length=2048), None, "RAW_OBJECT_TOO_LARGE"),
        (
            raw_metadata(),
            {"ContentLength": len(BODY) + 1, "Body": BodyStream(BODY)},
            "RAW_OBJECT_LENGTH_MISMATCH",
        ),
        (
            raw_metadata(sha256="0" * 64),
            {"ContentLength": len(BODY), "Body": BodyStream(BODY)},
            "RAW_OBJECT_DIGEST_MISMATCH",
        ),
    ],
)
def test_load_rejects_registry_or_object_integrity_mismatches(
    metadata: RawDocumentMetadata,
    response: Mapping[str, object] | None,
    code: str,
) -> None:
    api = StubS3Api(gets=[response] if response is not None else None)

    with pytest.raises(RawObjectIntegrityError) as raised:
        storage(api).load(metadata, max_bytes=1024)

    assert raised.value.code == code


def test_load_classifies_an_unreadable_s3_body() -> None:
    """Malformed SDK responses remain retryable without weakening integrity."""
    api = StubS3Api(gets=[{"ContentLength": len(BODY), "Body": object()}])

    with pytest.raises(RawObjectIntegrityError) as raised:
        storage(api).load(raw_metadata(), max_bytes=1024)

    assert raised.value.code == "RAW_OBJECT_BODY_INVALID"
    assert raised.value.retryable is True


def test_load_preserves_shared_s3_error_classification() -> None:
    """Raw-document reads retain the shared client's stable failure code."""
    api = StubS3Api(gets=[AwsError("SlowDown", 503)])

    with pytest.raises(RawObjectStorageError) as raised:
        storage(api).load(raw_metadata(), max_bytes=1024)

    assert raised.value.code == "S3_SLOWDOWN"
    assert raised.value.retryable is True


def corpus_write() -> NormalizedCorpusWrite:
    manifest = b'{"schema_version":"1"}\n'
    blocks = b"compressed-blocks"
    return NormalizedCorpusWrite(
        prefix="normalized/sec/320193/accession/sec-html-v2/source",
        manifest=manifest,
        manifest_sha256=sha256(manifest).hexdigest(),
        blocks=blocks,
        blocks_sha256=sha256(blocks).hexdigest(),
        filing_key="sec#accession",
        parser_version="sec-html-v2",
        source_sha256=DIGEST,
    )


def test_normalized_store_publishes_blocks_before_manifest() -> None:
    api = StubS3Api(puts=[{}, {}])
    client = S3NormalizedCorpusClient(api, bucket_name="filing-corpus-normalized")

    result = client.store(corpus_write())

    assert cast(str, api.put_calls[0]["Key"]).endswith("/blocks.jsonl.gz")
    assert api.put_calls[0]["ContentEncoding"] == "gzip"
    assert cast(str, api.put_calls[1]["Key"]).endswith("/manifest.json")
    assert "ContentEncoding" not in api.put_calls[1]
    assert all(call["IfNoneMatch"] == "*" for call in api.put_calls)
    assert result.reused_blocks is False
    assert result.reused_manifest is False


def test_normalized_store_reuses_an_identical_complete_corpus() -> None:
    request = corpus_write()
    api = StubS3Api(
        puts=[PreconditionFailed(), PreconditionFailed()],
        heads=[
            {
                "Metadata": {"sha256": request.blocks_sha256},
                "ContentLength": len(request.blocks),
            },
            {
                "Metadata": {"sha256": request.manifest_sha256},
                "ContentLength": len(request.manifest),
            },
        ],
    )

    result = S3NormalizedCorpusClient(
        api, bucket_name="filing-corpus-normalized"
    ).store(request)

    assert result.reused_blocks is True
    assert result.reused_manifest is True
    assert len(api.head_calls) == 2


def test_normalized_store_rejects_different_bytes_at_a_versioned_key() -> None:
    request = corpus_write()
    api = StubS3Api(
        puts=[PreconditionFailed()],
        heads=[{"Metadata": {"sha256": "0" * 64}, "ContentLength": 1}],
    )

    with pytest.raises(NormalizedObjectCollisionError):
        S3NormalizedCorpusClient(api, bucket_name="normalized").store(request)


def test_normalized_store_classifies_sdk_failures() -> None:
    api = StubS3Api(puts=[AwsError("SlowDown", 503)])

    with pytest.raises(NormalizedObjectStorageError) as raised:
        S3NormalizedCorpusClient(api, bucket_name="normalized").store(corpus_write())

    assert raised.value.retryable is True
