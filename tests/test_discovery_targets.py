"""Tests for versioned discovery-target contracts and S3 loading."""

import json
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path

import pytest

from filing_corpus_pipeline.discovery import (
    DiscoveryCompany,
    DiscoveryTargetReference,
    DiscoveryTargetSet,
    RegulatorRegistration,
)
from filing_corpus_pipeline.discovery.target_repository import (
    DiscoveryTargetRepository,
    InvalidDiscoveryTargetError,
    RetryableDiscoveryTargetError,
)
from filing_corpus_pipeline.storage.s3 import S3ObjectClient

ROOT = Path(__file__).resolve().parents[1]


class BodyStream:
    """Bounded in-memory stand-in for a botocore streaming body."""

    def __init__(self, body: bytes) -> None:
        self.body = body
        self.read_amounts: list[int | None] = []

    def read(self, amt: int | None = None) -> bytes:
        self.read_amounts.append(amt)
        return self.body if amt is None else self.body[:amt]


class FailingBodyStream:
    def read(self, amt: int | None = None) -> bytes:
        del amt
        raise OSError("stream interrupted")


class NonBytesBodyStream:
    def read(self, amt: int | None = None) -> bytes:
        del amt
        return "not bytes"  # type: ignore[return-value]


class StubS3Api:
    """Record the exact-version read and return a scripted response."""

    def __init__(self, response: Mapping[str, object] | Exception) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    def get_object(self, **kwargs: object) -> Mapping[str, object]:
        self.calls.append(kwargs)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class AwsError(Exception):
    """Minimal botocore-compatible error used for classification."""

    def __init__(self, code: str, status_code: int) -> None:
        super().__init__(code)
        self.response = {
            "Error": {"Code": code},
            "ResponseMetadata": {"HTTPStatusCode": status_code},
        }


def target_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "target_set_id": "portfolio-core",
        "revision": 1,
        "companies": [
            {
                "company_id": "apple-inc",
                "display_name": "Apple Inc.",
                "registrations": [
                    {
                        "regulator": "sec",
                        "issuer_id": "0000320193",
                        "filing_types": ["10-K", "10-Q"],
                    }
                ],
            }
        ],
    }


def reference(body: bytes) -> DiscoveryTargetReference:
    return DiscoveryTargetReference(
        bucket="target-config",
        key="discovery-targets/dev.json",
        version_id="version-7",
        sha256=sha256(body).hexdigest(),
    )


def repository(
    response: Mapping[str, object] | Exception,
    *,
    max_bytes: int = 256 * 1024,
) -> tuple[DiscoveryTargetRepository, StubS3Api]:
    """Compose the feature repository over the shared bounded S3 client."""
    api = StubS3Api(response)
    return (
        DiscoveryTargetRepository(
            S3ObjectClient(api),
            max_bytes=max_bytes,
        ),
        api,
    )


def test_repository_loads_and_validates_one_exact_object_version() -> None:
    body = json.dumps(target_payload()).encode()
    stream = BodyStream(body)
    targets, api = repository({"ContentLength": len(body), "Body": stream})

    result = targets.load(reference(body))

    assert result.target_set_id == "portfolio-core"
    assert result.companies[0].registrations[0].issuer_id == "0000320193"
    assert api.calls == [
        {
            "Bucket": "target-config",
            "Key": "discovery-targets/dev.json",
            "VersionId": "version-7",
        }
    ]
    assert stream.read_amounts == [256 * 1024 + 1]


def test_repository_rejects_an_integrity_mismatch() -> None:
    body = json.dumps(target_payload()).encode()
    wrong_reference = reference(body).model_copy(update={"sha256": "0" * 64})
    targets, _ = repository({"ContentLength": len(body), "Body": BodyStream(body)})

    with pytest.raises(InvalidDiscoveryTargetError, match="SHA-256"):
        targets.load(wrong_reference)


@pytest.mark.parametrize(
    ("response", "error_type", "message"),
    [
        (
            {"ContentLength": "1", "Body": BodyStream(b"x")},
            InvalidDiscoveryTargetError,
            "content length",
        ),
        (
            {"ContentLength": 5, "Body": BodyStream(b"12345")},
            InvalidDiscoveryTargetError,
            "exceeds",
        ),
        (
            {"ContentLength": 4, "Body": BodyStream(b"12345")},
            InvalidDiscoveryTargetError,
            "exceeds",
        ),
        (
            {"ContentLength": 1, "Body": object()},
            RetryableDiscoveryTargetError,
            "unreadable",
        ),
        (
            {"ContentLength": 1, "Body": FailingBodyStream()},
            RetryableDiscoveryTargetError,
            "body read failed",
        ),
        (
            {"ContentLength": 1, "Body": NonBytesBodyStream()},
            RetryableDiscoveryTargetError,
            "non-bytes",
        ),
        (
            {"ContentLength": 2, "Body": BodyStream(b"x")},
            RetryableDiscoveryTargetError,
            "length does not match",
        ),
    ],
)
def test_repository_rejects_invalid_or_incomplete_s3_responses(
    response: Mapping[str, object],
    error_type: type[Exception],
    message: str,
) -> None:
    targets, _ = repository(response, max_bytes=4)

    with pytest.raises(error_type, match=message):
        targets.load(reference(b"x"))


def test_repository_requires_a_positive_size_bound() -> None:
    with pytest.raises(ValueError, match="positive"):
        repository({}, max_bytes=0)


@pytest.mark.parametrize(
    "body",
    [
        b"not-json",
        json.dumps({**target_payload(), "schema_version": 2}).encode(),
        json.dumps({**target_payload(), "unexpected": True}).encode(),
    ],
)
def test_repository_rejects_invalid_json_or_schema(body: bytes) -> None:
    targets, _ = repository({"ContentLength": len(body), "Body": BodyStream(body)})

    with pytest.raises(InvalidDiscoveryTargetError):
        targets.load(reference(body))


def test_repository_classifies_transient_s3_errors_for_workflow_retry() -> None:
    targets, _ = repository(AwsError("SlowDown", 503))

    with pytest.raises(RetryableDiscoveryTargetError, match="SlowDown"):
        targets.load(reference(b"{}"))


def test_repository_classifies_missing_versions_as_permanent() -> None:
    targets, _ = repository(AwsError("NoSuchVersion", 404))

    with pytest.raises(InvalidDiscoveryTargetError, match="NoSuchVersion"):
        targets.load(reference(b"{}"))


def test_repository_treats_unknown_sdk_failures_as_transient() -> None:
    targets, _ = repository(OSError("offline"))

    with pytest.raises(RetryableDiscoveryTargetError, match="S3 object read failed"):
        targets.load(reference(b"{}"))


def test_target_set_rejects_cross_company_registration_collisions() -> None:
    registration = RegulatorRegistration(
        regulator="sec",
        issuer_id="0000320193",
        filing_types=("10-K",),
    )

    with pytest.raises(ValueError, match="exactly one company"):
        DiscoveryTargetSet(
            schema_version=1,
            target_set_id="portfolio-core",
            revision=1,
            companies=(
                DiscoveryCompany(
                    company_id="company-one",
                    display_name="Company One",
                    registrations=(registration,),
                ),
                DiscoveryCompany(
                    company_id="company-two",
                    display_name="Company Two",
                    registrations=(registration,),
                ),
            ),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", True),
        ("schema_version", "1"),
        ("revision", True),
        ("revision", 1.0),
    ],
)
def test_target_set_versions_require_json_integers(field: str, value: object) -> None:
    payload = target_payload()
    payload[field] = value

    with pytest.raises(ValueError, match="integer"):
        DiscoveryTargetSet.model_validate(payload)


@pytest.mark.parametrize(
    "registration",
    [
        {
            "regulator": "SEC",
            "issuer_id": "0000320193",
            "filing_types": ["10-K"],
        },
        {
            "regulator": "sec regulator",
            "issuer_id": "0000320193",
            "filing_types": ["10-K"],
        },
        {
            "regulator": "sec",
            "issuer_id": " 0000320193",
            "filing_types": ["10-K"],
        },
        {
            "regulator": "sec",
            "issuer_id": "0000320193",
            "filing_types": ["10-K", "10-K"],
        },
        {
            "regulator": "sec",
            "issuer_id": "0000320193",
            "filing_types": [" 10-K"],
        },
    ],
)
def test_registration_identity_and_filing_types_are_canonical(
    registration: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        RegulatorRegistration.model_validate(registration)


def test_source_controlled_development_target_set_matches_the_schema() -> None:
    directory = ROOT / "config" / "discovery-targets"
    paths = sorted(directory.glob("*.json"))

    assert paths
    target_sets = [
        DiscoveryTargetSet.model_validate_json(path.read_bytes()) for path in paths
    ]
    development_targets = next(
        targets
        for path, targets in zip(paths, target_sets, strict=True)
        if path.name == "dev.json"
    )

    assert all(targets.schema_version == 1 for targets in target_sets)
    assert {company.company_id for company in development_targets.companies} == {
        "apple-inc",
        "microsoft-corp",
    }
