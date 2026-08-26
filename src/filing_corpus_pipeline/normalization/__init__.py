"""Deterministic filing normalization feature."""

from filing_corpus_pipeline.normalization.artifacts import (
    BLOCKS_FILENAME,
    MANIFEST_FILENAME,
    NormalizationArtifacts,
    normalized_document_prefix,
    render_artifacts,
)
from filing_corpus_pipeline.normalization.models import (
    NORMALIZATION_SCHEMA_VERSION,
    SEC_HTML_PARSER_VERSION,
    BlockSection,
    BlockType,
    DocumentBlock,
    DocumentParseError,
    DocumentSection,
    NormalizationOutcome,
    NormalizationRequest,
    NormalizationResult,
    NormalizedDocument,
    ParseWarning,
    QualityStatus,
    RawFilingDocument,
    SecHtmlParserConfig,
)
from filing_corpus_pipeline.normalization.sec_html import SecHtmlNormalizer
from filing_corpus_pipeline.normalization.service import (
    NormalizationError,
    NormalizationService,
    PermanentNormalizationError,
    RetryableNormalizationError,
)

__all__ = [
    "BLOCKS_FILENAME",
    "MANIFEST_FILENAME",
    "NORMALIZATION_SCHEMA_VERSION",
    "SEC_HTML_PARSER_VERSION",
    "BlockSection",
    "BlockType",
    "DocumentBlock",
    "DocumentParseError",
    "DocumentSection",
    "NormalizationArtifacts",
    "NormalizationError",
    "NormalizationOutcome",
    "NormalizationRequest",
    "NormalizationResult",
    "NormalizationService",
    "NormalizedDocument",
    "ParseWarning",
    "PermanentNormalizationError",
    "QualityStatus",
    "RawFilingDocument",
    "RetryableNormalizationError",
    "SecHtmlNormalizer",
    "SecHtmlParserConfig",
    "normalized_document_prefix",
    "render_artifacts",
]
