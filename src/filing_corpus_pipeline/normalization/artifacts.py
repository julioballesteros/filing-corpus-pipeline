"""Deterministic artifact rendering for normalized filing documents."""

import gzip
import json
from dataclasses import dataclass
from hashlib import sha256
from urllib.parse import quote

from filing_corpus_pipeline.normalization.models import NormalizedDocument

MANIFEST_FILENAME = "manifest.json"
BLOCKS_FILENAME = "blocks.jsonl.gz"


@dataclass(frozen=True, slots=True)
class NormalizationArtifacts:
    """Versioned bytes ready for a later storage service to persist."""

    manifest: bytes
    blocks_jsonl_gzip: bytes
    manifest_sha256: str
    blocks_sha256: str


def normalized_document_prefix(document: NormalizedDocument) -> str:
    """Build a deterministic, source- and parser-versioned object prefix."""
    segments = (
        "normalized",
        document.filing.provider,
        document.filing.issuer.provider_issuer_id,
        document.filing.provider_filing_id,
        document.parser_version,
        document.source_sha256,
    )
    prefix = "/".join(quote(segment, safe="") for segment in segments)
    longest_key = f"{prefix}/{BLOCKS_FILENAME}"
    if len(longest_key.encode()) > 1024:
        raise ValueError("normalized document key exceeds S3's key size limit")
    return prefix


def render_artifacts(document: NormalizedDocument) -> NormalizationArtifacts:
    """Render reproducible JSONL and a self-describing manifest."""
    jsonl = b"".join(_json_bytes(block.to_dict()) + b"\n" for block in document.blocks)
    compressed_blocks = gzip.compress(jsonl, compresslevel=9, mtime=0)
    blocks_digest = sha256(compressed_blocks).hexdigest()

    manifest_value: dict[str, object] = {
        "schema_version": document.schema_version,
        "parser_version": document.parser_version,
        "filing_key": document.filing_key,
        "filing": document.filing.to_dict(),
        "source": {
            "sha256": document.source_sha256,
            "content_length": document.source_content_length,
            "content_type": document.source_content_type,
        },
        "document": {"title": document.title},
        "quality": {
            "status": document.quality_status.value,
            "warnings": [warning.to_dict() for warning in document.warnings],
        },
        "statistics": {
            "block_count": len(document.blocks),
            "section_count": len(document.sections),
            "table_count": document.table_count,
            "text_char_count": document.text_char_count,
        },
        "sections": [section.to_dict() for section in document.sections],
        "artifacts": {
            "blocks": {
                "filename": BLOCKS_FILENAME,
                "content_type": "application/x-ndjson",
                "content_encoding": "gzip",
                "sha256": blocks_digest,
                "record_count": len(document.blocks),
            }
        },
    }
    manifest = _json_bytes(manifest_value) + b"\n"
    return NormalizationArtifacts(
        manifest=manifest,
        blocks_jsonl_gzip=compressed_blocks,
        manifest_sha256=sha256(manifest).hexdigest(),
        blocks_sha256=blocks_digest,
    )


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
