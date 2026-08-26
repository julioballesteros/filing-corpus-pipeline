"""Tests for the normalization Lambda boundary and runtime configuration."""

from datetime import UTC, date, datetime, timedelta
from typing import cast

import pytest

from filing_corpus_pipeline.domain import FilingForm, FilingReference, IssuerReference
from filing_corpus_pipeline.entrypoints import normalization_lambda
from filing_corpus_pipeline.entrypoints.errors import LambdaConfigurationError
from filing_corpus_pipeline.normalization import (
    NormalizationRequest,
    NormalizationResult,
    NormalizationService,
    PermanentNormalizationError,
)
from filing_corpus_pipeline.normalization.models import NormalizationOutcome
from filing_corpus_pipeline.registry import NormalizedCorpusMetadata

NOW = datetime(2025, 8, 1, 18, 0, tzinfo=UTC)


def filing() -> FilingReference:
    return FilingReference(
        provider="sec",
        provider_filing_id="accession",
        issuer=IssuerReference("sec", "0000320193"),
        issuer_name="Apple Inc.",
        form=FilingForm.TEN_Q,
        filed_on=date(2025, 8, 1),
        report_date=None,
        accepted_at=None,
        primary_document="report.htm",
        filing_detail_url="https://example.test/index.html",
        primary_document_url="https://example.test/report.htm",
    )


def event() -> dict[str, object]:
    return {
        "filing": filing().to_dict(),
        "owner_id": "execution-1",
        "requested_at": NOW.isoformat(),
    }


def corpus() -> NormalizedCorpusMetadata:
    return NormalizedCorpusMetadata(
        bucket="normalized",
        prefix="normalized/prefix",
        manifest_key="normalized/prefix/manifest.json",
        manifest_sha256="a" * 64,
        blocks_key="normalized/prefix/blocks.jsonl.gz",
        blocks_sha256="b" * 64,
        parser_version="sec-html-v2",
        schema_version="1",
        block_count=3,
        section_count=1,
        warning_count=0,
        quality_status="PASS",
    )


class StubService:
    def __init__(self, result: NormalizationResult | Exception) -> None:
        self.result = result
        self.requests: list[NormalizationRequest] = []

    def normalize(self, request: NormalizationRequest) -> NormalizationResult:
        self.requests.append(request)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def configure(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "REGISTRY_TABLE_NAME": "registry",
        "RAW_BUCKET_NAME": "raw",
        "NORMALIZED_BUCKET_NAME": "normalized",
        "NORMALIZATION_LEASE_SECONDS": "600",
        "NORMALIZATION_MAX_DOCUMENT_BYTES": "26214400",
    }.items():
        monkeypatch.setenv(name, value)


def test_handler_builds_service_and_returns_bounded_corpus_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure(monkeypatch)
    stub = StubService(
        NormalizationResult(
            filing_key="sec#accession",
            outcome=NormalizationOutcome.NORMALIZED,
            attempt_count=1,
            corpus=corpus(),
        )
    )
    composition: list[dict[str, object]] = []

    def build(**kwargs: object) -> NormalizationService:
        composition.append(kwargs)
        return cast(NormalizationService, stub)

    monkeypatch.setattr(normalization_lambda, "build_sec_normalization_service", build)

    result = normalization_lambda.handler(event(), object())

    assert result["outcome"] == "NORMALIZED"
    assert result["corpus"] == corpus().to_dict()
    assert stub.requests[0].lease_duration == timedelta(minutes=10)
    assert composition == [
        {
            "registry_table_name": "registry",
            "raw_bucket_name": "raw",
            "normalized_bucket_name": "normalized",
            "max_document_bytes": 26214400,
        }
    ]


def test_handler_propagates_a_classified_service_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure(monkeypatch)
    stub = StubService(
        PermanentNormalizationError(
            "bad document",
            code="NO_CONTENT_BLOCKS",
            retryable=False,
        )
    )
    monkeypatch.setattr(
        normalization_lambda,
        "build_sec_normalization_service",
        lambda **_kwargs: cast(NormalizationService, stub),
    )

    with pytest.raises(PermanentNormalizationError):
        normalization_lambda.handler(event(), object())


@pytest.mark.parametrize(
    "invalid",
    [
        None,
        [],
        {},
        {"filing": {}, "owner_id": "owner", "requested_at": NOW.isoformat()},
        {"filing": filing().to_dict(), "owner_id": "", "requested_at": "bad"},
        {
            "filing": filing().to_dict(),
            "owner_id": "owner",
            "requested_at": "2025-08-01T18:00:00",
        },
    ],
)
def test_parse_rejects_invalid_map_items(invalid: object) -> None:
    with pytest.raises(normalization_lambda.InvalidNormalizationEvent):
        normalization_lambda.parse_normalization_event(
            invalid,
            lease_duration=timedelta(minutes=10),
        )


def test_handler_requires_every_runtime_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure(monkeypatch)
    monkeypatch.delenv("NORMALIZED_BUCKET_NAME")

    with pytest.raises(LambdaConfigurationError, match="NORMALIZED_BUCKET_NAME"):
        normalization_lambda.handler(event(), object())


@pytest.mark.parametrize("value", ["zero", "0", "52428801"])
def test_handler_rejects_an_invalid_document_limit(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    configure(monkeypatch)
    monkeypatch.setenv("NORMALIZATION_MAX_DOCUMENT_BYTES", value)

    with pytest.raises(LambdaConfigurationError):
        normalization_lambda.handler(event(), object())
