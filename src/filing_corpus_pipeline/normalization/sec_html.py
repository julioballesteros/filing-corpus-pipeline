"""Bounded normalization of SEC filing HTML into ordered corpus blocks."""

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256

from lxml import etree, html
from lxml.html import HtmlElement

from filing_corpus_pipeline.domain import DocumentPolicy
from filing_corpus_pipeline.normalization.models import (
    NORMALIZATION_SCHEMA_VERSION,
    SEC_HTML_PARSER_VERSION,
    BlockSection,
    BlockType,
    DocumentBlock,
    DocumentParseError,
    DocumentSection,
    NormalizedDocument,
    ParseWarning,
    RawFilingDocument,
    SecHtmlParserConfig,
)


class _SecFilingType(StrEnum):
    """SEC filing types with an implemented classification profile."""

    TEN_K = "10-K"
    TEN_Q = "10-Q"


_BLOCK_TAGS = frozenset(
    {
        "article",
        "blockquote",
        "div",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "p",
        "pre",
        "section",
        "table",
    }
)
_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_REMOVE_TAGS = frozenset(
    {
        "noscript",
        "script",
        "style",
        "svg",
        "template",
    }
)
_INLINE_XBRL_INFRASTRUCTURE_TAGS = frozenset(
    {"header", "hidden", "references", "resources"}
)
_PART_PATTERN = re.compile(r"^PART\s+(I|II|III|IV)\b", re.IGNORECASE)
_ITEM_PATTERN = re.compile(
    r"^ITEM\s+(\d{1,2}[A-Z]?)\b[\s.:\-\N{EN DASH}\N{EM DASH}]*(.*)$",
    re.IGNORECASE,
)

_TEN_K_SECTIONS = {
    ("I", "1"): "business",
    ("I", "1A"): "risk_factors",
    ("I", "1B"): "unresolved_staff_comments",
    ("I", "1C"): "cybersecurity",
    ("I", "2"): "properties",
    ("I", "3"): "legal_proceedings",
    ("I", "4"): "mine_safety_disclosures",
    ("II", "5"): "market_for_registrants_equity",
    ("II", "6"): "reserved",
    ("II", "7"): "management_discussion_and_analysis",
    ("II", "7A"): "market_risk",
    ("II", "8"): "financial_statements",
    ("II", "9"): "changes_in_accountants",
    ("II", "9A"): "controls_and_procedures",
    ("II", "9B"): "other_information",
    ("II", "9C"): "foreign_jurisdiction_disclosures",
    ("III", "10"): "directors_and_governance",
    ("III", "11"): "executive_compensation",
    ("III", "12"): "security_ownership",
    ("III", "13"): "related_transactions",
    ("III", "14"): "principal_accountant_fees",
    ("IV", "15"): "exhibits_and_schedules",
    ("IV", "16"): "form_10k_summary",
}
_ITEM_TITLES = {
    _SecFilingType.TEN_K: {
        ("I", "1"): ("business",),
        ("I", "1A"): ("risk factors",),
        ("I", "1B"): ("unresolved staff comments",),
        ("I", "1C"): ("cybersecurity",),
        ("I", "2"): ("properties",),
        ("I", "3"): ("legal proceedings",),
        ("I", "4"): ("mine safety disclosures",),
        ("II", "5"): (
            "market for registrants common equity related stockholder matters "
            "and issuer purchases of equity securities",
        ),
        ("II", "6"): ("reserved",),
        ("II", "7"): (
            "managements discussion and analysis",
            "managements discussion and analysis of financial condition and "
            "results of operations",
        ),
        ("II", "7A"): ("quantitative and qualitative disclosures about market risk",),
        ("II", "8"): (
            "financial statements and supplementary data",
            "financial statements and supplemental data",
        ),
        ("II", "9"): (
            "changes in and disagreements with accountants on accounting and "
            "financial disclosure",
        ),
        ("II", "9A"): ("controls and procedures",),
        ("II", "9B"): ("other information",),
        ("II", "9C"): (
            "disclosure regarding foreign jurisdictions that prevent inspections",
            "disclosures regarding foreign jurisdictions that prevent inspections",
        ),
        ("III", "10"): (
            "directors executive officers and corporate governance",
            "directors and corporate governance",
        ),
        ("III", "11"): ("executive compensation",),
        ("III", "12"): (
            "security ownership of certain beneficial owners and management and "
            "related stockholder matters",
        ),
        ("III", "13"): (
            "certain relationships and related transactions and director independence",
            "related transactions and director independence",
        ),
        ("III", "14"): ("principal accountant fees and services",),
        ("IV", "15"): (
            "exhibits and financial statement schedules",
            "exhibits financial statement schedules",
            "exhibits and schedules",
        ),
        ("IV", "16"): ("form 10 k summary",),
    },
    _SecFilingType.TEN_Q: {
        ("I", "1"): ("financial statements",),
        ("I", "2"): (
            "managements discussion and analysis",
            "managements discussion and analysis of financial condition and "
            "results of operations",
        ),
        ("I", "3"): ("quantitative and qualitative disclosures about market risk",),
        ("I", "4"): ("controls and procedures",),
        ("II", "1"): ("legal proceedings",),
        ("II", "1A"): ("risk factors",),
        ("II", "2"): (
            "unregistered sales of equity securities and use of proceeds",
            "unregistered sales of equity securities",
        ),
        ("II", "3"): ("defaults upon senior securities",),
        ("II", "4"): ("mine safety disclosures",),
        ("II", "5"): ("other information",),
        ("II", "6"): ("exhibits",),
    },
}
_TEN_Q_SECTIONS = {
    ("I", "1"): "financial_statements",
    ("I", "2"): "management_discussion_and_analysis",
    ("I", "3"): "market_risk",
    ("I", "4"): "controls_and_procedures",
    ("II", "1"): "legal_proceedings",
    ("II", "1A"): "risk_factors",
    ("II", "2"): "unregistered_equity_sales",
    ("II", "3"): "defaults",
    ("II", "4"): "mine_safety",
    ("II", "5"): "other_information",
    ("II", "6"): "exhibits",
}
_EXPECTED_SECTIONS = {
    _SecFilingType.TEN_K: frozenset(
        {
            "financial_statements",
            "management_discussion_and_analysis",
            "risk_factors",
        }
    ),
    _SecFilingType.TEN_Q: frozenset(
        {"financial_statements", "management_discussion_and_analysis"}
    ),
}


@dataclass(frozen=True, slots=True)
class _ExtractedBlock:
    block_type: BlockType
    text: str
    table_rows: tuple[tuple[str, ...], ...] | None
    is_toc_link: bool
    is_emphasized: bool


@dataclass(frozen=True, slots=True)
class _SectionContext:
    section_id: str
    part: str | None
    item: str | None
    canonical_name: str
    heading: str


class SecHtmlNormalizer:
    """Convert one SEC primary HTML document into deterministic local output."""

    def __init__(self, config: SecHtmlParserConfig | None = None) -> None:
        self._config = config or SecHtmlParserConfig()

    @property
    def parser_version(self) -> str:
        """Return the version used for idempotency and safe reprocessing."""
        return SEC_HTML_PARSER_VERSION

    def normalize(self, source: RawFilingDocument) -> NormalizedDocument:
        """Parse, classify, and validate one raw filing without external I/O."""
        if source.filing.provider != "sec":
            raise DocumentParseError(
                f"SEC normalizer cannot process provider {source.filing.provider!r}",
                code="UNSUPPORTED_PROVIDER",
            )
        if source.filing.document_policy is not DocumentPolicy.PRIMARY:
            raise DocumentParseError(
                "SEC normalizer requires a resolved primary document",
                code="UNSUPPORTED_DOCUMENT_POLICY",
            )
        try:
            filing_type = _SecFilingType(source.filing.filing_type)
        except ValueError as error:
            raise DocumentParseError(
                f"unsupported SEC filing type: {source.filing.filing_type!r}",
                code="UNSUPPORTED_SEC_FILING_TYPE",
            ) from error
        if not source.body:
            raise DocumentParseError(
                "source document is empty",
                code="EMPTY_SOURCE_DOCUMENT",
            )
        if len(source.body) > self._config.max_input_bytes:
            raise DocumentParseError(
                "source document exceeds the configured byte limit",
                code="DOCUMENT_TOO_LARGE",
            )

        source_digest = sha256(source.body).hexdigest()
        if (
            source.expected_sha256 is not None
            and source_digest != source.expected_sha256
        ):
            raise DocumentParseError(
                "source document digest does not match acquisition metadata",
                code="SOURCE_DIGEST_MISMATCH",
            )

        root = self._parse(source.body)
        self._remove_non_content(root)
        title = (
            self._title(root)
            or f"{source.filing.issuer_name} {source.filing.filing_type}"
        )
        extracted = self._extract_blocks(root)
        if not extracted:
            raise DocumentParseError(
                "source document contains no visible content blocks",
                code="NO_CONTENT_BLOCKS",
            )

        blocks, warnings = self._classify_blocks(extracted, filing_type)
        sections = self._summarize_sections(blocks)
        warnings.extend(self._quality_warnings(blocks, filing_type))

        return NormalizedDocument(
            filing_key=source.filing_key,
            filing=source.filing,
            parser_version=SEC_HTML_PARSER_VERSION,
            schema_version=NORMALIZATION_SCHEMA_VERSION,
            source_sha256=source_digest,
            source_content_length=len(source.body),
            source_content_type=source.content_type,
            title=title,
            blocks=tuple(blocks),
            sections=tuple(sections),
            warnings=tuple(warnings),
        )

    @staticmethod
    def _parse(body: bytes) -> HtmlElement:
        parser = html.HTMLParser(
            recover=True,
            no_network=True,
            remove_comments=True,
            huge_tree=False,
        )
        try:
            return html.document_fromstring(body, parser=parser)
        except (etree.ParserError, etree.XMLSyntaxError, ValueError) as error:
            raise DocumentParseError(
                "source document could not be parsed as bounded HTML",
                code="INVALID_HTML",
            ) from error

    @staticmethod
    def _remove_non_content(root: HtmlElement) -> None:
        for element in list(root.iter()):
            if element is root:
                continue
            local_name = _local_name(element)
            style = "".join(element.get("style", "").lower().split())
            hidden = (
                element.get("hidden") is not None
                or "display:none" in style
                or "visibility:hidden" in style
            )
            if (
                local_name in _REMOVE_TAGS
                or _is_inline_xbrl_infrastructure(element)
                or hidden
            ):
                element.drop_tree()

    def _extract_blocks(self, root: HtmlElement) -> list[_ExtractedBlock]:
        blocks: list[_ExtractedBlock] = []
        table_cells = 0
        for element in root.iter():
            tag = _local_name(element)
            if tag not in _BLOCK_TAGS or _has_table_ancestor(element):
                continue
            if tag != "table" and _has_block_descendant(element):
                continue

            table_rows: tuple[tuple[str, ...], ...] | None = None
            is_emphasized = _is_fully_emphasized(element)
            if tag == "table":
                remaining_cells = self._config.max_table_cells - table_cells
                table_rows, cell_count = _table_rows(
                    element,
                    max_cells=remaining_cells,
                )
                table_cells += cell_count
                if not table_rows:
                    continue
                text = "\n".join(" | ".join(row) for row in table_rows)
                block_type = BlockType.TABLE
                layout_heading = _layout_table_heading(
                    table_rows,
                    is_emphasized=is_emphasized,
                )
                if layout_heading is not None:
                    text = layout_heading
                    block_type = BlockType.HEADING
                    table_rows = None
            else:
                text = _normalized_text(" ".join(element.itertext()))
                if not text:
                    continue
                block_type = _block_type(tag)

            blocks.append(
                _ExtractedBlock(
                    block_type=block_type,
                    text=text,
                    table_rows=table_rows,
                    is_toc_link=_is_toc_link(element, text),
                    is_emphasized=is_emphasized,
                )
            )
            if len(blocks) > self._config.max_blocks:
                raise DocumentParseError(
                    "document exceeds the configured block limit",
                    code="PARSER_RESOURCE_LIMIT",
                )

        if not blocks:
            body = next(
                (element for element in root.iter() if _local_name(element) == "body"),
                root,
            )
            text = _normalized_text(" ".join(body.itertext()))
            if text:
                blocks.append(
                    _ExtractedBlock(
                        BlockType.PARAGRAPH,
                        text,
                        None,
                        False,
                        False,
                    )
                )
        return blocks

    @staticmethod
    def _title(root: HtmlElement) -> str | None:
        for element in root.iter():
            if _local_name(element) == "title":
                title = _normalized_text(" ".join(element.itertext()))
                if title:
                    return title
        return None

    @staticmethod
    def _classify_blocks(
        extracted: list[_ExtractedBlock], form: _SecFilingType
    ) -> tuple[list[DocumentBlock], list[ParseWarning]]:
        blocks: list[DocumentBlock] = []
        warnings: list[ParseWarning] = []
        current = _SectionContext("preamble", None, None, "preamble", "Preamble")
        current_part: str | None = None
        item_occurrences: Counter[tuple[str | None, str]] = Counter()

        for ordinal, value in enumerate(extracted):
            part_match = _part_heading_match(value)
            item_match = _item_heading_match(value, form, current_part)
            block_type = value.block_type

            if part_match is not None:
                current_part = part_match.group(1).upper()
                current = _SectionContext(
                    section_id=f"part-{current_part.lower()}",
                    part=current_part,
                    item=None,
                    canonical_name="part",
                    heading=value.text,
                )
                block_type = BlockType.HEADING
            elif item_match is not None:
                item = item_match.group(1).upper()
                identity = (current_part, item)
                item_occurrences[identity] += 1
                occurrence = item_occurrences[identity]
                base_id = _item_section_id(current_part, item)
                section_id = base_id if occurrence == 1 else f"{base_id}-{occurrence}"
                if occurrence > 1:
                    warnings.append(
                        ParseWarning(
                            code="DUPLICATE_ITEM_HEADING",
                            message=f"encountered {base_id} more than once",
                        )
                    )
                current = _SectionContext(
                    section_id=section_id,
                    part=current_part,
                    item=item,
                    canonical_name=_canonical_section(form, current_part, item),
                    heading=value.text,
                )
                block_type = BlockType.HEADING

            digest = sha256(value.text.encode()).hexdigest()
            blocks.append(
                DocumentBlock(
                    block_id=f"block-{ordinal:05d}-{digest[:12]}",
                    ordinal=ordinal,
                    block_type=block_type,
                    text=value.text,
                    content_sha256=digest,
                    section=BlockSection(
                        id=current.section_id,
                        part=current.part,
                        item=current.item,
                        canonical_name=current.canonical_name,
                        heading=current.heading,
                    ),
                    table_rows=value.table_rows,
                )
            )
        return blocks, warnings

    @staticmethod
    def _summarize_sections(blocks: list[DocumentBlock]) -> list[DocumentSection]:
        grouped: dict[str, list[DocumentBlock]] = {}
        for block in blocks:
            grouped.setdefault(block.section_id, []).append(block)

        return [
            DocumentSection(
                section_id=section_id,
                part=section_blocks[0].part,
                item=section_blocks[0].item,
                canonical_name=section_blocks[0].canonical_section,
                heading=section_blocks[0].section_heading,
                first_ordinal=section_blocks[0].ordinal,
                block_count=len(section_blocks),
                text_length=sum(len(block.text) for block in section_blocks),
            )
            for section_id, section_blocks in grouped.items()
        ]

    def _quality_warnings(
        self, blocks: list[DocumentBlock], form: _SecFilingType
    ) -> list[ParseWarning]:
        warnings: list[ParseWarning] = []
        text_length = sum(len(block.text) for block in blocks)
        if text_length < self._config.short_document_chars:
            warnings.append(
                ParseWarning(
                    code="SHORT_DOCUMENT",
                    message=(
                        f"normalized content has {text_length} characters; "
                        f"expected at least {self._config.short_document_chars}"
                    ),
                )
            )

        item_sections = {
            block.canonical_section for block in blocks if block.item is not None
        }
        if not item_sections:
            warnings.append(
                ParseWarning(
                    code="NO_ITEM_SECTIONS",
                    message="no SEC Item headings were identified",
                )
            )
        for missing in sorted(_EXPECTED_SECTIONS[form] - item_sections):
            warnings.append(
                ParseWarning(
                    code="MISSING_EXPECTED_SECTION",
                    message=(
                        f"expected {form.value} section was not identified: {missing}"
                    ),
                )
            )
        return warnings


def _local_name(element: HtmlElement) -> str:
    tag = element.tag
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].rsplit(":", 1)[-1].lower()


def _is_inline_xbrl_infrastructure(element: HtmlElement) -> bool:
    tag = element.tag
    if not isinstance(tag, str):
        return False
    qualified_tag = tag.lower()
    has_inline_xbrl_namespace = (
        "inline-xbrl" in qualified_tag
        or "inlinexbrl" in qualified_tag
        or qualified_tag.startswith("ix:")
    )
    return (
        has_inline_xbrl_namespace
        and _local_name(element) in _INLINE_XBRL_INFRASTRUCTURE_TAGS
    )


def _normalized_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).replace("\xa0", " ").split())


def _has_table_ancestor(element: HtmlElement) -> bool:
    return any(_local_name(ancestor) == "table" for ancestor in element.iterancestors())


def _has_block_descendant(element: HtmlElement) -> bool:
    return any(
        _local_name(descendant) in _BLOCK_TAGS
        for descendant in element.iterdescendants()
    )


def _block_type(tag: str) -> BlockType:
    if tag in _HEADING_TAGS:
        return BlockType.HEADING
    if tag == "li":
        return BlockType.LIST_ITEM
    if tag == "pre":
        return BlockType.PREFORMATTED
    return BlockType.PARAGRAPH


def _part_heading_match(value: _ExtractedBlock) -> re.Match[str] | None:
    if (
        value.is_toc_link
        or len(value.text) > 100
        or value.block_type in {BlockType.LIST_ITEM, BlockType.TABLE}
    ):
        return None
    match = _PART_PATTERN.match(value.text)
    if match is None:
        return None
    remainder = value.text[match.end() :].strip(" .:-\N{EN DASH}\N{EM DASH}")
    has_heading_evidence = (
        value.block_type is BlockType.HEADING
        or value.is_emphasized
        or value.text.isupper()
        or not remainder
    )
    return match if has_heading_evidence else None


def _item_heading_match(
    value: _ExtractedBlock,
    form: _SecFilingType,
    part: str | None,
) -> re.Match[str] | None:
    if (
        value.is_toc_link
        or len(value.text) > 300
        or value.block_type
        in {BlockType.LIST_ITEM, BlockType.TABLE, BlockType.PREFORMATTED}
    ):
        return None
    match = _ITEM_PATTERN.match(value.text)
    if match is None:
        return None
    item = match.group(1).upper()
    has_heading_evidence = (
        value.block_type is BlockType.HEADING
        or value.is_emphasized
        or value.text.isupper()
        or _matches_item_title(form, part, item, match.group(2))
    )
    return match if has_heading_evidence else None


def _matches_item_title(
    form: _SecFilingType,
    part: str | None,
    item: str,
    title: str,
) -> bool:
    if part is None:
        return False
    normalized = _title_key(title)
    return normalized in _ITEM_TITLES[form].get((part, item), ())


def _title_key(value: str) -> str:
    normalized = (
        unicodedata.normalize("NFKC", value)
        .casefold()
        .replace("\N{RIGHT SINGLE QUOTATION MARK}", "'")
        .replace("'", "")
    )
    return " ".join(re.sub(r"[^a-z0-9]+", " ", normalized).split())


def _layout_table_heading(
    rows: tuple[tuple[str, ...], ...],
    *,
    is_emphasized: bool,
) -> str | None:
    if len(rows) != 1 or not is_emphasized:
        return None
    nonempty_cells = [cell for cell in rows[0] if cell]
    if not 1 <= len(nonempty_cells) <= 2:
        return None
    text = _normalized_text(" ".join(nonempty_cells))
    if len(text) > 300:
        return None
    if _PART_PATTERN.match(text) is None and _ITEM_PATTERN.match(text) is None:
        return None
    return text


def _is_fully_emphasized(element: HtmlElement) -> bool:
    emphasized_characters = 0
    total_characters = 0

    def visit(node: HtmlElement, inherited: bool) -> None:
        nonlocal emphasized_characters, total_characters
        current = inherited or _element_is_emphasized(node)
        if node.text:
            length = len(_normalized_text(node.text))
            total_characters += length
            emphasized_characters += length if current else 0
        for child in node.iterchildren():
            visit(child, current)
            if child.tail:
                length = len(_normalized_text(child.tail))
                total_characters += length
                emphasized_characters += length if current else 0

    inherited = any(
        _element_is_emphasized(ancestor) for ancestor in element.iterancestors()
    )
    visit(element, inherited)
    return total_characters > 0 and emphasized_characters / total_characters >= 0.8


def _element_is_emphasized(element: HtmlElement) -> bool:
    if _local_name(element) in {"b", "strong"}:
        return True
    style = "".join(element.get("style", "").casefold().split())
    return re.search(r"font-weight:(?:bold|[6-9]00)(?:;|$)", style) is not None


def _table_rows(
    element: HtmlElement, *, max_cells: int
) -> tuple[tuple[tuple[str, ...], ...], int]:
    rows: list[tuple[str, ...]] = []
    cell_count = 0
    for row in element.iterdescendants():
        if _local_name(row) != "tr" or _nearest_table(row) is not element:
            continue
        cells = [
            _normalized_text(" ".join(child.itertext()))
            for child in row.iterchildren()
            if _local_name(child) in {"th", "td"}
        ]
        cell_count += len(cells)
        if cell_count > max_cells:
            raise DocumentParseError(
                "document exceeds the configured table-cell limit",
                code="PARSER_RESOURCE_LIMIT",
            )
        if any(cells):
            rows.append(tuple(cells))
    return tuple(rows), cell_count


def _nearest_table(element: HtmlElement) -> HtmlElement | None:
    return next(
        (
            ancestor
            for ancestor in element.iterancestors()
            if _local_name(ancestor) == "table"
        ),
        None,
    )


def _is_toc_link(element: HtmlElement, text: str) -> bool:
    linked_text = ""
    has_fragment_link = False
    for descendant in element.iterdescendants():
        if _local_name(descendant) != "a":
            continue
        href = descendant.get("href", "").strip()
        if "#" in href and href.rsplit("#", 1)[-1]:
            has_fragment_link = True
            linked_text += _normalized_text(" ".join(descendant.itertext()))
    return has_fragment_link and len(linked_text) >= max(1, int(len(text) * 0.8))


def _item_section_id(part: str | None, item: str) -> str:
    part_segment = part.lower() if part is not None else "unknown"
    return f"part-{part_segment}-item-{item.lower()}"


def _canonical_section(form: _SecFilingType, part: str | None, item: str) -> str:
    mappings = _TEN_K_SECTIONS if form is _SecFilingType.TEN_K else _TEN_Q_SECTIONS
    return mappings.get((part or "", item), f"item_{item.lower()}")
