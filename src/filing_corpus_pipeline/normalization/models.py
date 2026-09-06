"""Contracts for deterministic raw-filing normalization."""

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

from pydantic import AwareDatetime, Field, model_validator

from filing_corpus_pipeline.domain import FilingReference
from filing_corpus_pipeline.models import (
    LowercaseSha256Digest,
    NonEmptyString,
    PipelineModel,
)
from filing_corpus_pipeline.registry.models import NormalizedCorpusMetadata

NORMALIZATION_SCHEMA_VERSION = "2"
SEC_HTML_PARSER_VERSION = "sec-html-v3"


class BlockType(StrEnum):
    """Semantic block types emitted by the bounded HTML parser."""

    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST_ITEM = "list_item"
    TABLE = "table"
    PREFORMATTED = "preformatted"


class QualityStatus(StrEnum):
    """Document-level validation result."""

    PASS = "PASS"
    WARN = "WARN"


class NormalizationOutcome(StrEnum):
    """Terminal or duplicate-safe result of one normalization invocation."""

    NORMALIZED = "NORMALIZED"
    ALREADY_COMPLETED = "ALREADY_COMPLETED"
    ALREADY_IN_PROGRESS = "ALREADY_IN_PROGRESS"
    NOT_RETRYABLE = "NOT_RETRYABLE"


class NormalizationRequest(PipelineModel):
    """Request to normalize one acquired filing under a bounded lease."""

    filing: FilingReference
    owner_id: NonEmptyString
    requested_at: AwareDatetime
    lease_duration: timedelta = Field(
        gt=timedelta(0),
        le=timedelta(days=1),
    )


class NormalizationResult(PipelineModel):
    """Bounded workflow result pointing to committed corpus artifacts."""

    filing_key: NonEmptyString
    outcome: NormalizationOutcome
    attempt_count: int = Field(gt=0)
    corpus: NormalizedCorpusMetadata | None = None

    @model_validator(mode="after")
    def _validate_corpus_disposition(self) -> "NormalizationResult":
        has_corpus = self.outcome in {
            NormalizationOutcome.NORMALIZED,
            NormalizationOutcome.ALREADY_COMPLETED,
        }
        if has_corpus != (self.corpus is not None):
            raise ValueError(
                "corpus metadata is required only for completed normalization"
            )
        return self


class ParseWarning(PipelineModel):
    """A bounded data-quality finding that does not discard the document."""

    code: NonEmptyString = Field(max_length=100)
    message: NonEmptyString = Field(max_length=1000)


class RawFilingDocument(PipelineModel):
    """Raw object bytes plus the provenance needed to normalize them."""

    filing_key: NonEmptyString
    filing: FilingReference
    body: bytes
    content_type: NonEmptyString
    expected_sha256: LowercaseSha256Digest | None = None


class BlockSection(PipelineModel):
    """Section identity embedded in each serialized document block."""

    id: NonEmptyString
    part: str | None
    item: str | None
    canonical_name: NonEmptyString
    heading: NonEmptyString


class DocumentBlock(PipelineModel):
    """One ordered, independently addressable unit of normalized content."""

    block_id: NonEmptyString
    ordinal: int = Field(ge=0)
    block_type: BlockType = Field(serialization_alias="type")
    text: NonEmptyString
    content_sha256: LowercaseSha256Digest
    section: BlockSection
    table_rows: tuple[tuple[str, ...], ...] | None = None

    @model_validator(mode="after")
    def _validate_table_rows(self) -> "DocumentBlock":
        if (self.block_type is BlockType.TABLE) != (self.table_rows is not None):
            raise ValueError("table_rows must be present only for table blocks")
        if self.table_rows is not None and not self.table_rows:
            raise ValueError("table_rows must not be empty")
        return self

    @property
    def section_id(self) -> str:
        return self.section.id

    @property
    def part(self) -> str | None:
        return self.section.part

    @property
    def item(self) -> str | None:
        return self.section.item

    @property
    def canonical_section(self) -> str:
        return self.section.canonical_name

    @property
    def section_heading(self) -> str:
        return self.section.heading


class DocumentSection(PipelineModel):
    """A compact index entry for a contiguous set of blocks."""

    section_id: NonEmptyString = Field(serialization_alias="id")
    part: str | None
    item: str | None
    canonical_name: NonEmptyString
    heading: NonEmptyString
    first_ordinal: int = Field(ge=0)
    block_count: int = Field(gt=0)
    text_length: int = Field(gt=0)


class NormalizedDocument(PipelineModel):
    """Versioned, provider-neutral corpus document produced from one filing."""

    filing_key: NonEmptyString
    filing: FilingReference
    parser_version: NonEmptyString
    schema_version: NonEmptyString
    source_sha256: LowercaseSha256Digest
    source_content_length: int = Field(gt=0)
    source_content_type: NonEmptyString
    title: NonEmptyString
    blocks: tuple[DocumentBlock, ...] = Field(min_length=1)
    sections: tuple[DocumentSection, ...] = Field(min_length=1)
    warnings: tuple[ParseWarning, ...] = ()

    @model_validator(mode="after")
    def _validate_block_ordinals(self) -> "NormalizedDocument":
        if tuple(block.ordinal for block in self.blocks) != tuple(
            range(len(self.blocks))
        ):
            raise ValueError("block ordinals must be contiguous from zero")
        return self

    @property
    def quality_status(self) -> QualityStatus:
        return QualityStatus.WARN if self.warnings else QualityStatus.PASS

    @property
    def text_char_count(self) -> int:
        return sum(len(block.text) for block in self.blocks)

    @property
    def table_count(self) -> int:
        return sum(block.block_type is BlockType.TABLE for block in self.blocks)


class DocumentParseError(RuntimeError):
    """A bounded, classified normalization failure."""

    def __init__(self, message: str, *, code: str, retryable: bool = False) -> None:
        if not code.strip() or len(code) > 100:
            raise ValueError("parse error code must contain at most 100 characters")
        if not message.strip() or len(message) > 1000:
            raise ValueError("parse error message must contain at most 1000 characters")
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class SecHtmlParserConfig:
    """Explicit memory and output bounds for one parser invocation."""

    max_input_bytes: int = 25 * 1024 * 1024
    max_blocks: int = 50_000
    max_table_cells: int = 250_000
    short_document_chars: int = 1_000

    def __post_init__(self) -> None:
        for field_name in (
            "max_input_bytes",
            "max_blocks",
            "max_table_cells",
            "short_document_chars",
        ):
            if getattr(self, field_name) < 1:
                raise ValueError(f"{field_name} must be positive")
