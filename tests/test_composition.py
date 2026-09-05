"""Tests for feature-local runtime composition roots."""

from types import ModuleType

import pytest

from filing_corpus_pipeline.acquisition import AcquisitionService
from filing_corpus_pipeline.acquisition import composition as acquisition_composition
from filing_corpus_pipeline.discovery import DiscoveryService
from filing_corpus_pipeline.discovery import composition as discovery_composition
from filing_corpus_pipeline.discovery.composition import build_sec_discovery_service
from filing_corpus_pipeline.normalization import NormalizationService
from filing_corpus_pipeline.normalization import (
    composition as normalization_composition,
)


def test_discovery_composition_builds_the_sec_service() -> None:
    """The discovery entrypoints share one feature-local dependency graph."""
    assert isinstance(
        build_sec_discovery_service("pipeline contact@example.com"),
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

    monkeypatch.setattr(discovery_composition, "_aws_client", aws_client)

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

    monkeypatch.setattr(acquisition_composition, "_aws_client", aws_client)

    service = acquisition_composition.build_sec_acquisition_service(
        user_agent="pipeline contact@example.com",
        registry_table_name="filing-registry",
        raw_bucket_name="filing-corpus-raw",
        max_document_bytes=1024,
    )

    assert isinstance(service, AcquisitionService)
    assert services == ["dynamodb", "s3"]


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

    monkeypatch.setattr(acquisition_composition, "import_module", import_module)

    assert acquisition_composition._aws_client("s3") is not None
    assert requested_modules == ["boto3"]
    assert clients == ["s3"]


def test_normalization_composition_shares_one_s3_client() -> None:
    """The normalization graph requests DynamoDB and one low-level S3 client."""
    services: list[str] = []

    def aws_client(service_name: str) -> object:
        services.append(service_name)
        return object()

    original = normalization_composition._aws_client
    normalization_composition._aws_client = aws_client
    try:
        service = normalization_composition.build_sec_normalization_service(
            registry_table_name="registry",
            raw_bucket_name="raw",
            normalized_bucket_name="normalized",
            max_document_bytes=1024,
        )
    finally:
        normalization_composition._aws_client = original

    assert isinstance(service, NormalizationService)
    assert services == ["dynamodb", "s3"]
