"""Tests for feature-local runtime composition roots."""

from types import ModuleType

import pytest

from filing_corpus_pipeline.acquisition import AcquisitionService
from filing_corpus_pipeline.acquisition import composition as acquisition_composition
from filing_corpus_pipeline.discovery import DiscoveryService
from filing_corpus_pipeline.discovery import composition as discovery_composition
from filing_corpus_pipeline.discovery.composition import build_discovery_service
from filing_corpus_pipeline.normalization import NormalizationService
from filing_corpus_pipeline.normalization import (
    composition as normalization_composition,
)
from filing_corpus_pipeline.runtime import aws as runtime_aws
from filing_corpus_pipeline.runtime.config import LambdaConfigurationError


def test_discovery_composition_builds_the_sec_service() -> None:
    """The discovery callers share one feature-local dependency graph."""
    assert isinstance(
        build_discovery_service(sec_user_agent="pipeline contact@example.com"),
        DiscoveryService,
    )


def test_discovery_composition_reads_source_runtime_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SEC_USER_AGENT", "pipeline contact@example.com")

    assert isinstance(
        build_discovery_service(),
        DiscoveryService,
    )


def test_discovery_composition_builds_the_s3_target_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The discovery graph requests S3 only for its deployed target manifest."""
    services: list[str] = []

    def aws_client(service_name: str) -> object:
        services.append(service_name)
        return object()

    monkeypatch.setattr(discovery_composition, "aws_client", aws_client)

    discovery_composition.build_discovery_target_repository()

    assert services == ["s3"]


def test_acquisition_composition_builds_aws_storage_and_sec_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The acquisition graph requests exactly its two low-level AWS clients."""
    services: list[str] = []

    def aws_client(service_name: str) -> object:
        services.append(service_name)
        return object()

    monkeypatch.setattr(acquisition_composition, "aws_client", aws_client)

    service = acquisition_composition.build_acquisition_service(
        sec_user_agent="pipeline contact@example.com",
        registry_table_name="filing-registry",
        raw_bucket_name="filing-corpus-raw",
        max_document_bytes=1024,
        max_filing_detail_bytes=2048,
    )

    assert isinstance(service, AcquisitionService)
    assert services == ["dynamodb", "s3"]


def test_acquisition_composition_reads_source_runtime_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Source credentials and policy stay inside the composition root."""
    monkeypatch.setenv("SEC_USER_AGENT", "pipeline contact@example.com")
    monkeypatch.setattr(acquisition_composition, "aws_client", lambda _: object())

    assert isinstance(
        acquisition_composition.build_acquisition_service(
            registry_table_name="filing-registry",
            raw_bucket_name="filing-corpus-raw",
            max_document_bytes=1024,
            max_filing_detail_bytes=2048,
        ),
        AcquisitionService,
    )


def test_acquisition_composition_requires_source_runtime_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing SEC setting fails at the source-composition boundary."""
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)

    with pytest.raises(LambdaConfigurationError, match="SEC_USER_AGENT"):
        acquisition_composition.build_acquisition_service(
            registry_table_name="filing-registry",
            raw_bucket_name="filing-corpus-raw",
            max_document_bytes=1024,
            max_filing_detail_bytes=2048,
        )


def test_acquisition_composition_loads_the_managed_runtime_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """boto3 remains a Lambda-runtime dependency rather than a source ZIP import."""
    requested_modules: list[str] = []
    module = ModuleType("boto3")
    clients: list[str] = []

    def client(service_name: str) -> object:
        clients.append(service_name)
        return object()

    module.client = client  # type: ignore[attr-defined]

    def import_module(name: str) -> ModuleType:
        requested_modules.append(name)
        return module

    monkeypatch.setattr(runtime_aws, "import_module", import_module)

    assert runtime_aws.aws_client("s3") is not None
    assert requested_modules == ["boto3"]
    assert clients == ["s3"]


def test_normalization_composition_shares_one_s3_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The normalization graph requests DynamoDB and one low-level S3 client."""
    services: list[str] = []

    def aws_client(service_name: str) -> object:
        services.append(service_name)
        return object()

    monkeypatch.setattr(normalization_composition, "aws_client", aws_client)
    service = normalization_composition.build_sec_normalization_service(
        registry_table_name="registry",
        raw_bucket_name="raw",
        normalized_bucket_name="normalized",
        max_document_bytes=1024,
    )

    assert isinstance(service, NormalizationService)
    assert services == ["dynamodb", "s3"]
