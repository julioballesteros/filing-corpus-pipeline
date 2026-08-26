"""Contracts for deterministic raw-filing normalization."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from filing_corpus_pipeline.domain import FilingReference
from filing_corpus_pipeline.registry.models import NormalizedCorpusMetadata

NORMALIZATION_SCHEMA_VERSION = "1"
SEC_HTML_PARSER_VERSION = "sec-html-v2"


def _require_text(value: str, *, field: str) -> None:
    if not value.strip():
        raise ValueError(f"{field} must not be empty")


def _require_sha256(value: str, *, field: str) -> None:
    if len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")


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


@dataclass(frozen=True, slots=True)
class NormalizationRequest:
    """Request to normalize one acquired filing under a bounded lease."""

    filing: FilingReference
    owner_id: str
    requested_at: datetime
    lease_duration: timedelta

    def __post_init__(self) -> None:
        _require_text(self.owner_id, field="owner_id")
        if self.requested_at.tzinfo is None or self.requested_at.utcoffset() is None:
            raise ValueError("requested_at must include a timezone offset")
        if self.lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        if self.lease_duration > timedelta(days=1):
            raise ValueError("lease_duration must not exceed one day")


@dataclass(frozen=True, slots=True)
class NormalizationResult:
    """Bounded workflow result pointing to committed corpus artifacts."""

    filing_key: str
    outcome: NormalizationOutcome
    attempt_count: int
    corpus: NormalizedCorpusMetadata | None = None

    def __post_init__(self) -> None:
        _require_text(self.filing_key, field="filing_key")
        if self.attempt_count < 1:
            raise ValueError("attempt_count must be positive")
        has_corpus = self.outcome in {
            NormalizationOutcome.NORMALIZED,
            NormalizationOutcome.ALREADY_COMPLETED,
        }
        if has_corpus != (self.corpus is not None):
            raise ValueError(
                "corpus metadata is required only for completed normalization"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "filing_key": self.filing_key,
            "outcome": self.outcome.value,
            "attempt_count": self.attempt_count,
            "corpus": self.corpus.to_dict() if self.corpus is not None else None,
        }


@dataclass(frozen=True, slots=True)
class ParseWarning:
    """A bounded data-quality finding that does not discard the document."""

    code: str
    message: str

    def __post_init__(self) -> None:
        if not self.code.strip() or len(self.code) > 100:
            raise ValueError("warning code must contain at most 100 characters")
        if not self.message.strip() or len(self.message) > 1000:
            raise ValueError("warning message must contain at most 1000 characters")

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True, slots=True)
class RawFilingDocument:
    """Raw object bytes plus the provenance needed to normalize them."""

    filing_key: str
    filing: FilingReference
    body: bytes
    content_type: str
    expected_sha256: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.filing_key, field="filing_key")
        _require_text(self.content_type, field="content_type")
        if self.expected_sha256 is not None:
            _require_sha256(self.expected_sha256, field="expected_sha256")


@dataclass(frozen=True, slots=True)
class DocumentBlock:
    """One ordered, independently addressable unit of normalized content."""

    block_id: str
    ordinal: int
    block_type: BlockType
    text: str
    content_sha256: str
    section_id: str
    part: str | None
    item: str | None
    canonical_section: str
    section_heading: str
    table_rows: tuple[tuple[str, ...], ...] | None = None

    def __post_init__(self) -> None:
        _require_text(self.block_id, field="block_id")
        if self.ordinal < 0:
            raise ValueError("ordinal must not be negative")
        _require_text(self.text, field="text")
        _require_sha256(self.content_sha256, field="content_sha256")
        _require_text(self.section_id, field="section_id")
        _require_text(self.canonical_section, field="canonical_section")
        _require_text(self.section_heading, field="section_heading")
        if (self.block_type is BlockType.TABLE) != (self.table_rows is not None):
            raise ValueError("table_rows must be present only for table blocks")
        if self.table_rows is not None and not self.table_rows:
            raise ValueError("table_rows must not be empty")

    def to_dict(self) -> dict[str, object]:
        return {
            "block_id": self.block_id,
            "ordinal": self.ordinal,
            "type": self.block_type.value,
            "text": self.text,
            "content_sha256": self.content_sha256,
            "section": {
                "id": self.section_id,
                "part": self.part,
                "item": self.item,
                "canonical_name": self.canonical_section,
                "heading": self.section_heading,
            },
            "table_rows": (
                [list(row) for row in self.table_rows]
                if self.table_rows is not None
                else None
            ),
        }


@dataclass(frozen=True, slots=True)
class DocumentSection:
    """A compact index entry for a contiguous set of blocks."""

    section_id: str
    part: str | None
    item: str | None
    canonical_name: str
    heading: str
    first_ordinal: int
    block_count: int
    text_length: int

    def __post_init__(self) -> None:
        _require_text(self.section_id, field="section_id")
        _require_text(self.canonical_name, field="canonical_name")
        _require_text(self.heading, field="heading")
        if self.first_ordinal < 0:
            raise ValueError("first_ordinal must not be negative")
        if self.block_count < 1:
            raise ValueError("block_count must be positive")
        if self.text_length < 1:
            raise ValueError("text_length must be positive")

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.section_id,
            "part": self.part,
            "item": self.item,
            "canonical_name": self.canonical_name,
            "heading": self.heading,
            "first_ordinal": self.first_ordinal,
            "block_count": self.block_count,
            "text_length": self.text_length,
        }


@dataclass(frozen=True, slots=True)
class NormalizedDocument:
    """Versioned, provider-neutral corpus document produced from one filing."""

    filing_key: str
    filing: FilingReference
    parser_version: str
    schema_version: str
    source_sha256: str
    source_content_length: int
    source_content_type: str
    title: str
    blocks: tuple[DocumentBlock, ...]
    sections: tuple[DocumentSection, ...]
    warnings: tuple[ParseWarning, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.filing_key, field="filing_key")
        _require_text(self.parser_version, field="parser_version")
        _require_text(self.schema_version, field="schema_version")
        _require_sha256(self.source_sha256, field="source_sha256")
        if self.source_content_length < 1:
            raise ValueError("source_content_length must be positive")
        _require_text(self.source_content_type, field="source_content_type")
        _require_text(self.title, field="title")
        if not self.blocks:
            raise ValueError("blocks must not be empty")
        if not self.sections:
            raise ValueError("sections must not be empty")
        if tuple(block.ordinal for block in self.blocks) != tuple(
            range(len(self.blocks))
        ):
            raise ValueError("block ordinals must be contiguous from zero")

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
