"""Tests for bounded, deterministic SEC primary-document normalization."""

from datetime import date
from hashlib import sha256
from pathlib import Path

import pytest
from lxml import etree
from lxml import html as lxml_html

from filing_corpus_pipeline.domain import FilingReference
from filing_corpus_pipeline.normalization import (
    BlockType,
    DocumentParseError,
    QualityStatus,
    RawFilingDocument,
    SecHtmlNormalizer,
    SecHtmlParserConfig,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "sec"


def _filing(filing_type: str) -> FilingReference:
    return FilingReference(
        company_id="example-issuer",
        provider="sec",
        provider_filing_id="0000000000-25-000001",
        provider_issuer_id="0000000000",
        issuer_name="Example Issuer",
        filing_type=filing_type,
        filed_on=date(2025, 4, 30),
        report_date=date(2025, 3, 31),
        accepted_at=None,
        primary_document="report.htm",
        filing_detail_url="https://www.sec.gov/Archives/example-index.html",
        primary_document_url="https://www.sec.gov/Archives/report.htm",
    )


def _source(
    body: bytes,
    *,
    filing_type: str = "10-Q",
    expected_sha256: str | None = None,
) -> RawFilingDocument:
    return RawFilingDocument(
        filing_key="sec#0000000000-25-000001",
        filing=_filing(filing_type),
        body=body,
        content_type="text/html; charset=utf-8",
        expected_sha256=expected_sha256,
    )


def _normalizer(**overrides: int) -> SecHtmlNormalizer:
    values = {
        "max_input_bytes": 25 * 1024 * 1024,
        "max_blocks": 50_000,
        "max_table_cells": 250_000,
        "short_document_chars": 1,
    }
    values.update(overrides)
    return SecHtmlNormalizer(SecHtmlParserConfig(**values))


def test_normalizes_inline_xbrl_10q_without_infrastructure_content() -> None:
    body = (_FIXTURES / "10q-inline-xbrl-sample.html").read_bytes()
    document = _normalizer().normalize(
        _source(body, expected_sha256=sha256(body).hexdigest())
    )
    all_text = "\n".join(block.text for block in document.blocks)

    assert document.title == "Example Holdings — Quarterly Report"
    assert document.source_sha256 == sha256(body).hexdigest()
    assert document.source_content_length == len(body)
    assert document.quality_status is QualityStatus.PASS
    assert [block.ordinal for block in document.blocks] == list(
        range(len(document.blocks))
    )
    assert "SCRIPT_SENTINEL" not in all_text
    assert "HIDDEN_XBRL_SENTINEL" not in all_text
    assert "REFERENCE_SENTINEL" not in all_text
    assert "NOSCRIPT_SENTINEL" not in all_text
    assert "CSS_HIDDEN_SENTINEL" not in all_text
    assert "125,000" in all_text

    item_sections = {
        (block.part, block.item): block.canonical_section
        for block in document.blocks
        if block.item is not None
    }
    assert item_sections == {
        ("I", "1"): "financial_statements",
        ("I", "2"): "management_discussion_and_analysis",
        ("II", "1A"): "risk_factors",
    }
    assert not any(
        warning.code == "DUPLICATE_ITEM_HEADING" for warning in document.warnings
    )


def test_preserves_table_structure_and_visible_inline_xbrl_fact() -> None:
    body = (_FIXTURES / "10q-inline-xbrl-sample.html").read_bytes()
    document = _normalizer().normalize(_source(body))
    tables = [block for block in document.blocks if block.block_type is BlockType.TABLE]

    assert len(tables) == 2
    assert tables[1].table_rows == (
        ("Metric", "Current quarter"),
        ("Revenue", "125,000"),
    )
    assert tables[1].canonical_section == "financial_statements"
    serialized_rows = tables[1].model_dump(mode="json", by_alias=True)["table_rows"]
    assert isinstance(serialized_rows, list)
    assert serialized_rows[-1] == ["Revenue", "125,000"]


def test_classifies_part_specific_10k_sections_and_keeps_html_header() -> None:
    body = (_FIXTURES / "10k-inline-xbrl-sample.html").read_bytes()
    document = _normalizer().normalize(_source(body, filing_type="10-K"))
    item_sections = {
        (block.part, block.item): block.canonical_section
        for block in document.blocks
        if block.item is not None
    }

    assert item_sections == {
        ("I", "1"): "business",
        ("I", "1A"): "risk_factors",
        ("II", "7"): "management_discussion_and_analysis",
        ("II", "8"): "financial_statements",
    }
    assert any("public filing" in block.text for block in document.blocks)
    assert document.quality_status is QualityStatus.PASS


@pytest.mark.parametrize(
    ("source", "config", "code"),
    [
        (_source(b""), {}, "EMPTY_SOURCE_DOCUMENT"),
        (_source(b"<p>too large</p>"), {"max_input_bytes": 5}, "DOCUMENT_TOO_LARGE"),
        (
            _source(b"<html><body><script>hidden</script></body></html>"),
            {},
            "NO_CONTENT_BLOCKS",
        ),
        (
            _source(b"<html><body><p>one</p><p>two</p></body></html>"),
            {"max_blocks": 1},
            "PARSER_RESOURCE_LIMIT",
        ),
        (
            _source(b"<table><tr><td>one</td><td>two</td></tr></table>"),
            {"max_table_cells": 1},
            "PARSER_RESOURCE_LIMIT",
        ),
    ],
)
def test_rejects_empty_or_resource_unsafe_documents(
    source: RawFilingDocument,
    config: dict[str, int],
    code: str,
) -> None:
    with pytest.raises(DocumentParseError) as raised:
        _normalizer(**config).normalize(source)

    assert raised.value.code == code
    assert raised.value.retryable is False


def test_rejects_source_digest_mismatch() -> None:
    with pytest.raises(DocumentParseError) as raised:
        _normalizer().normalize(_source(b"<p>content</p>", expected_sha256="0" * 64))

    assert raised.value.code == "SOURCE_DIGEST_MISMATCH"


def test_rejects_a_filing_type_without_a_normalization_profile() -> None:
    with pytest.raises(DocumentParseError) as raised:
        _normalizer().normalize(
            _source(
                b"<html><body><p>Current report</p></body></html>", filing_type="8-K"
            )
        )

    assert raised.value.code == "UNSUPPORTED_SEC_FILING_TYPE"


def test_recovers_malformed_html_and_falls_back_to_issuer_title() -> None:
    document = _normalizer().normalize(_source(b"<html><body><div><p>Visible content"))

    assert document.title == "Example Issuer 10-Q"
    assert document.blocks[0].text == "Visible content"


def test_plain_body_text_uses_a_fallback_block() -> None:
    document = _normalizer().normalize(
        _source(b"<html><body>Plain body text</body></html>")
    )

    assert len(document.blocks) == 1
    assert document.blocks[0].block_type is BlockType.PARAGRAPH
    assert document.blocks[0].text == "Plain body text"


def test_quality_warnings_preserve_parseable_but_incomplete_content() -> None:
    document = SecHtmlNormalizer(
        SecHtmlParserConfig(short_document_chars=100)
    ).normalize(_source(b"<p>A short unsectioned filing.</p>"))
    warning_codes = [warning.code for warning in document.warnings]

    assert document.quality_status is QualityStatus.WARN
    assert warning_codes == [
        "SHORT_DOCUMENT",
        "NO_ITEM_SECTIONS",
        "MISSING_EXPECTED_SECTION",
        "MISSING_EXPECTED_SECTION",
    ]


def test_duplicate_items_are_separate_stable_sections() -> None:
    body = b"""
        <h2>PART I</h2>
        <p>ITEM 1. FINANCIAL STATEMENTS</p><p>First version.</p>
        <p>ITEM 1. FINANCIAL STATEMENTS</p><p>Second version.</p>
        <p>ITEM 2. MANAGEMENT'S DISCUSSION AND ANALYSIS</p><p>Discussion.</p>
    """
    document = _normalizer().normalize(_source(body))

    assert {block.section_id for block in document.blocks} >= {
        "part-i-item-1",
        "part-i-item-1-2",
    }
    assert [warning.code for warning in document.warnings] == ["DUPLICATE_ITEM_HEADING"]


def test_fragment_link_item_is_not_treated_as_a_section_heading() -> None:
    body = b"""
        <div><a href="report.htm#item1">ITEM 1. FINANCIAL STATEMENTS</a></div>
        <h2>PART I</h2><h3 id="item1">ITEM 1. FINANCIAL STATEMENTS</h3>
        <p>Statements.</p>
        <h3>ITEM 2. MANAGEMENT'S DISCUSSION AND ANALYSIS</h3><p>Discussion.</p>
    """
    document = _normalizer().normalize(_source(body))

    assert not any(
        warning.code == "DUPLICATE_ITEM_HEADING" for warning in document.warnings
    )
    assert document.blocks[0].section_id == "preamble"
    assert document.blocks[0].block_type is BlockType.PARAGRAPH


def test_handles_layout_headings_without_promoting_cross_references() -> None:
    body = (_FIXTURES / "layout-heading-regressions.html").read_bytes()
    document = _normalizer().normalize(_source(body))
    headings = [
        block for block in document.blocks if block.block_type is BlockType.HEADING
    ]
    references = [block for block in document.blocks if "CROSS_REFERENCE" in block.text]
    data_table = next(
        block
        for block in document.blocks
        if block.block_type is BlockType.TABLE
        and "Not a structural heading" in block.text
    )

    assert [
        (block.part, block.item, block.canonical_section) for block in headings
    ] == [
        ("I", None, "part"),
        ("I", "1", "financial_statements"),
        ("I", "2", "management_discussion_and_analysis"),
        ("II", None, "part"),
        ("II", "1A", "risk_factors"),
    ]
    assert all(block.table_rows is None for block in headings)
    assert all(" | " not in block.text for block in headings)
    assert [block.block_type for block in references] == [
        BlockType.PARAGRAPH,
        BlockType.PARAGRAPH,
    ]
    assert references[0].section_id == "part-i-item-1"
    assert references[1].section_id == "part-i-item-2"
    assert data_table.table_rows == (
        ("Item 1.", "Financial Statements"),
        ("Reference", "Not a structural heading"),
    )
    assert not any(
        warning.code == "DUPLICATE_ITEM_HEADING" for warning in document.warnings
    )


def test_maps_10k_part_one_operational_sections() -> None:
    body = b"""
        <h2>PART I</h2>
        <p>Item 1. Business</p><p>Business content.</p>
        <p>Item 1A. Risk Factors</p><p>Risks.</p>
        <p>Item 2. Properties</p><p>Properties content.</p>
        <p>Item 3. Legal Proceedings</p><p>Legal content.</p>
        <p>Item 4. Mine Safety Disclosures</p><p>Safety content.</p>
        <h2>PART II</h2>
        <p>Item 7. Management's Discussion and Analysis</p><p>Discussion.</p>
        <p>Item 8. Financial Statements and Supplementary Data</p><p>Statements.</p>
    """
    document = _normalizer().normalize(_source(body, filing_type="10-K"))
    item_sections = {
        (block.part, block.item): block.canonical_section
        for block in document.blocks
        if block.item is not None
    }

    assert item_sections[("I", "2")] == "properties"
    assert item_sections[("I", "3")] == "legal_proceedings"
    assert item_sections[("I", "4")] == "mine_safety_disclosures"


def test_long_item_like_paragraph_is_not_treated_as_a_section_heading() -> None:
    body = (
        b"<h2>PART I</h2><p>ITEM 1. "
        + b"narrative text " * 30
        + b"</p><h3>ITEM 2. MANAGEMENT'S DISCUSSION AND ANALYSIS</h3>"
        + b"<p>Discussion.</p><h3>ITEM 1. FINANCIAL STATEMENTS</h3>"
    )
    document = _normalizer().normalize(_source(body))

    long_paragraph = document.blocks[1]
    assert len(long_paragraph.text) > 300
    assert long_paragraph.block_type is BlockType.PARAGRAPH
    assert long_paragraph.item is None


def test_table_preserves_empty_cell_positions() -> None:
    body = b"""
        <h2>PART I</h2><h3>ITEM 1. FINANCIAL STATEMENTS</h3>
        <table><tr><td>Revenue</td><td></td><td>10</td></tr></table>
        <h3>ITEM 2. MANAGEMENT'S DISCUSSION AND ANALYSIS</h3>
    """
    document = _normalizer().normalize(_source(body))
    table = next(
        block for block in document.blocks if block.block_type is BlockType.TABLE
    )

    assert table.table_rows == (("Revenue", "", "10"),)


def test_emits_list_preformatted_and_unknown_item_types() -> None:
    body = b"""
        <h2>PART III</h2><h3>ITEM 99. Other</h3>
        <ul><li>First point</li></ul><pre>preserved   text</pre>
    """
    document = _normalizer().normalize(_source(body))

    assert [block.block_type for block in document.blocks[-2:]] == [
        BlockType.LIST_ITEM,
        BlockType.PREFORMATTED,
    ]
    assert document.blocks[-1].canonical_section == "item_99"


def test_classifies_unexpected_parser_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_parse(*args: object, **kwargs: object) -> None:
        raise etree.ParserError("invalid")

    monkeypatch.setattr(lxml_html, "document_fromstring", fail_parse)
    with pytest.raises(DocumentParseError) as raised:
        _normalizer().normalize(_source(b"<p>content</p>"))

    assert raised.value.code == "INVALID_HTML"
