"""Versioned company and regulator targets for filing discovery."""

from datetime import date
from typing import Literal

from pydantic import Field, field_validator, model_validator

from filing_corpus_pipeline.discovery.models import DiscoveryResult
from filing_corpus_pipeline.models import (
    LowercaseSha256Digest,
    NonEmptyString,
    PipelineModel,
)


def _canonical_slug(value: str) -> str:
    if value != value.strip().lower() or any(
        character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in value
    ):
        raise ValueError("must contain only lowercase letters, digits, and hyphens")
    if value.startswith("-") or value.endswith("-"):
        raise ValueError("must not start or end with a hyphen")
    return value


class DiscoveryTargetReference(PipelineModel):
    """Immutable identity of one deployed target-set object."""

    bucket: NonEmptyString
    key: NonEmptyString
    version_id: NonEmptyString
    sha256: LowercaseSha256Digest


class RegulatorRegistration(PipelineModel):
    """One company's filing-discovery identity at one regulator."""

    regulator: NonEmptyString
    issuer_id: NonEmptyString
    filing_types: tuple[NonEmptyString, ...] = Field(min_length=1)

    @field_validator("regulator")
    @classmethod
    def _validate_regulator(cls, value: str) -> str:
        return _canonical_slug(value)

    @field_validator("issuer_id")
    @classmethod
    def _validate_issuer_id(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("must not contain surrounding space")
        return value

    @model_validator(mode="after")
    def _validate_unique_filing_types(self) -> "RegulatorRegistration":
        if any(value != value.strip() for value in self.filing_types):
            raise ValueError("filing_types must not contain surrounding space")
        if len(set(self.filing_types)) != len(self.filing_types):
            raise ValueError("filing_types must not contain duplicates")
        return self


class DiscoveryCompany(PipelineModel):
    """Stable company identity and its regulator registrations."""

    company_id: NonEmptyString
    display_name: NonEmptyString
    registrations: tuple[RegulatorRegistration, ...] = Field(min_length=1)

    @field_validator("company_id")
    @classmethod
    def _validate_company_id(cls, value: str) -> str:
        return _canonical_slug(value)

    @model_validator(mode="after")
    def _validate_unique_registrations(self) -> "DiscoveryCompany":
        identities = [
            (registration.regulator, registration.issuer_id)
            for registration in self.registrations
        ]
        if len(set(identities)) != len(identities):
            raise ValueError("registrations must have unique regulator/issuer pairs")
        return self


class DiscoveryTargetSet(PipelineModel):
    """Source-controlled discovery targets with an evolvable schema."""

    schema_version: Literal[1]
    target_set_id: NonEmptyString
    revision: int = Field(ge=1)
    description: NonEmptyString | None = None
    companies: tuple[DiscoveryCompany, ...] = Field(min_length=1)

    @field_validator("schema_version", "revision", mode="before")
    @classmethod
    def _validate_integer_versions(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("must be an integer")
        return value

    @field_validator("target_set_id")
    @classmethod
    def _validate_target_set_id(cls, value: str) -> str:
        return _canonical_slug(value)

    @model_validator(mode="after")
    def _validate_unique_identities(self) -> "DiscoveryTargetSet":
        company_ids = [company.company_id for company in self.companies]
        if len(set(company_ids)) != len(company_ids):
            raise ValueError("companies must have unique company_id values")

        registrations = [
            (registration.regulator, registration.issuer_id)
            for company in self.companies
            for registration in company.registrations
        ]
        if len(set(registrations)) != len(registrations):
            raise ValueError(
                "a regulator/issuer pair must belong to exactly one company"
            )
        return self


class DiscoveryWindow(PipelineModel):
    """Inclusive filing-date window shared by all targets in one execution."""

    filed_from: date
    filed_to: date

    @model_validator(mode="after")
    def _validate_date_range(self) -> "DiscoveryWindow":
        if self.filed_from > self.filed_to:
            raise ValueError("filed_from must be on or before filed_to")
        return self


class DiscoveryInvocation(PipelineModel):
    """Resolved execution window and immutable target configuration reference."""

    window: DiscoveryWindow
    target_config: DiscoveryTargetReference


class DiscoveryTargetProvenance(PipelineModel):
    """Target-set identity retained in workflow results for replay and audit."""

    target_set_id: NonEmptyString
    revision: int = Field(ge=1)
    schema_version: int = Field(ge=1)
    bucket: NonEmptyString
    key: NonEmptyString
    version_id: NonEmptyString
    sha256: LowercaseSha256Digest


class TargetedDiscoveryResult(DiscoveryResult):
    """Discovery result qualified by the exact deployed target-set version."""

    target_set: DiscoveryTargetProvenance


def target_provenance(
    target_set: DiscoveryTargetSet,
    reference: DiscoveryTargetReference,
) -> DiscoveryTargetProvenance:
    """Combine logical target metadata with its immutable object identity."""
    return DiscoveryTargetProvenance(
        target_set_id=target_set.target_set_id,
        revision=target_set.revision,
        schema_version=target_set.schema_version,
        bucket=reference.bucket,
        key=reference.key,
        version_id=reference.version_id,
        sha256=reference.sha256,
    )
