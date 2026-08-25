"""Tests for idempotent raw-document S3 storage."""

import base64
from collections.abc import Mapping
from hashlib import sha256
from typing import ClassVar

import pytest

from filing_corpus_pipeline.storage import (
    RawObjectCollisionError,
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
    ) -> None:
        self.puts = list(puts or [{}])
        self.heads = list(heads or [])
        self.put_calls: list[dict[str, object]] = []
        self.head_calls: list[dict[str, object]] = []

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


BODY = b"<html>filing</html>"
DIGEST = sha256(BODY).hexdigest()


def write_request() -> RawObjectWrite:
    """Build a complete object write with source provenance."""
    return RawObjectWrite(
        key="raw/sec/320193/accession/report.htm",
        body=BODY,
        sha256=DIGEST,
        content_type="text/html",
        filing_key="sec#accession",
        source_url="https://www.sec.gov/report.htm",
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
        "source_url": "https://www.sec.gov/report.htm",
    }
    values.update(overrides)

    with pytest.raises(ValueError):
        RawObjectWrite(**values)  # type: ignore[arg-type]


def test_storage_requires_a_bucket_name() -> None:
    """A missing runtime setting is caught during composition."""
    with pytest.raises(ValueError):
        S3RawDocumentClient(StubS3Api(), bucket_name="")
