"""Tests for provider-neutral normalized-corpus contracts."""

from datetime import date

import pytest

from filing_corpus_pipeline.domain import FilingReference
from filing_corpus_pipeline.normalization import (
    SEC_HTML_PARSER_VERSION,
    BlockSection,
    BlockType,
    DocumentBlock,
    DocumentParseError,
    DocumentSection,
    NormalizedDocument,
    ParseWarning,
    QualityStatus,
    RawFilingDocument,
    SecHtmlParserConfig,
)


def _filing() -> FilingReference:
    return FilingReference(
        company_id="example-issuer",
        provider="sec",
        provider_filing_id="0000000000-25-000001",
        provider_issuer_id="0000000000",
        issuer_name="Example Issuer",
        filing_type="10-Q",
        filed_on=date(2025, 4, 30),
        report_date=date(2025, 3, 31),
        accepted_at=None,
        primary_document="report.htm",
        filing_detail_url="https://www.sec.gov/Archives/example-index.html",
        primary_document_url="https://www.sec.gov/Archives/report.htm",
    )


def _block(**overrides: object) -> DocumentBlock:
    values: dict[str, object] = {
        "block_id": "block-00000-aaaaaaaaaaaa",
        "ordinal": 0,
        "block_type": BlockType.PARAGRAPH,
        "text": "Visible filing content.",
        "content_sha256": "a" * 64,
        "section": BlockSection(
            id="preamble",
            part=None,
            item=None,
            canonical_name="preamble",
            heading="Preamble",
        ),
    }
    values.update(overrides)
    return DocumentBlock(**values)  # type: ignore[arg-type]


def _section(**overrides: object) -> DocumentSection:
    values: dict[str, object] = {
        "section_id": "preamble",
        "part": None,
        "item": None,
        "canonical_name": "preamble",
        "heading": "Preamble",
        "first_ordinal": 0,
        "block_count": 1,
        "text_length": 23,
    }
    values.update(overrides)
    return DocumentSection(**values)  # type: ignore[arg-type]


def _document(**overrides: object) -> NormalizedDocument:
    values: dict[str, object] = {
        "filing_key": "sec#0000000000-25-000001",
        "filing": _filing(),
        "parser_version": SEC_HTML_PARSER_VERSION,
        "schema_version": "1",
        "source_sha256": "b" * 64,
        "source_content_length": 100,
        "source_content_type": "text/html",
        "title": "Example report",
        "blocks": (_block(),),
        "sections": (_section(),),
    }
    values.update(overrides)
    return NormalizedDocument(**values)  # type: ignore[arg-type]


def test_block_and_section_have_json_compatible_contracts() -> None:
    """Blocks retain provenance and table structure without HTML."""
    block = _block(
        block_type=BlockType.TABLE,
        table_rows=(("Metric", "Value"),),
    )

    assert block.model_dump(mode="json", by_alias=True) == {
        "block_id": "block-00000-aaaaaaaaaaaa",
        "ordinal": 0,
        "type": "table",
        "text": "Visible filing content.",
        "content_sha256": "a" * 64,
        "section": {
            "id": "preamble",
            "part": None,
            "item": None,
            "canonical_name": "preamble",
            "heading": "Preamble",
        },
        "table_rows": [["Metric", "Value"]],
    }
    assert _section().model_dump(mode="json", by_alias=True)["block_count"] == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"block_id": ""},
        {"ordinal": -1},
        {"text": ""},
        {"content_sha256": "invalid"},
        {
            "section": {
                "id": "",
                "part": None,
                "item": None,
                "canonical_name": "preamble",
                "heading": "Preamble",
            }
        },
        {
            "section": {
                "id": "preamble",
                "part": None,
                "item": None,
                "canonical_name": "",
                "heading": "Preamble",
            }
        },
        {
            "section": {
                "id": "preamble",
                "part": None,
                "item": None,
                "canonical_name": "preamble",
                "heading": "",
            }
        },
        {"block_type": BlockType.TABLE},
        {"table_rows": (("unexpected",),)},
        {"block_type": BlockType.TABLE, "table_rows": ()},
    ],
)
def test_block_rejects_invalid_contracts(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        _block(**overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        {"section_id": ""},
        {"canonical_name": ""},
        {"heading": ""},
        {"first_ordinal": -1},
        {"block_count": 0},
        {"text_length": 0},
    ],
)
def test_section_rejects_invalid_contracts(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        _section(**overrides)


def test_normalized_document_exposes_statistics_and_quality() -> None:
    warning = ParseWarning(
        code="SHORT_DOCUMENT",
        message="content is shorter than expected",
    )
    document = _document(warnings=(warning,))

    assert document.quality_status is QualityStatus.WARN
    assert document.text_char_count == len("Visible filing content.")
    assert document.table_count == 0
    assert warning.model_dump(mode="json")["code"] == "SHORT_DOCUMENT"
    assert _document().quality_status is QualityStatus.PASS


@pytest.mark.parametrize(
    "overrides",
    [
        {"filing_key": ""},
        {"parser_version": ""},
        {"schema_version": ""},
        {"source_sha256": "bad"},
        {"source_content_length": 0},
        {"source_content_type": ""},
        {"title": ""},
        {"blocks": ()},
        {"sections": ()},
        {"blocks": (_block(ordinal=1),)},
    ],
)
def test_normalized_document_rejects_invalid_contracts(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        _document(**overrides)


@pytest.mark.parametrize(
    "values",
    [
        {"filing_key": ""},
        {"content_type": ""},
        {"expected_sha256": "not-a-digest"},
    ],
)
def test_raw_source_requires_valid_provenance(values: dict[str, object]) -> None:
    arguments: dict[str, object] = {
        "filing_key": "sec#filing",
        "filing": _filing(),
        "body": b"body",
        "content_type": "text/html",
    }
    arguments.update(values)
    with pytest.raises(ValueError):
        RawFilingDocument(**arguments)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("code", "message"),
    [("", "failure"), ("ERROR", ""), ("x" * 101, "failure"), ("ERROR", "x" * 1001)],
)
def test_parser_diagnostics_are_bounded(code: str, message: str) -> None:
    with pytest.raises(ValueError):
        DocumentParseError(message, code=code)
    with pytest.raises(ValueError):
        ParseWarning(code=code, message=message)


@pytest.mark.parametrize(
    "field",
    ["max_input_bytes", "max_blocks", "max_table_cells", "short_document_chars"],
)
def test_parser_config_requires_positive_resource_bounds(field: str) -> None:
    with pytest.raises(ValueError, match=field):
        SecHtmlParserConfig(**{field: 0})
