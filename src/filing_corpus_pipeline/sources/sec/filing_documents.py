"""Typed parsing for SEC filing-detail document inventories."""

from dataclasses import dataclass
from html.parser import HTMLParser

from filing_corpus_pipeline.sources.sec.identifiers import sec_filing_document_url


@dataclass(frozen=True, slots=True)
class SecFilingDocument:
    """One document advertised by an SEC filing-detail page."""

    sequence: int
    description: str | None
    document_name: str
    document_type: str
    size_bytes: int
    source_url: str


class SecFilingDocumentResponseError(RuntimeError):
    """A filing-detail document table violates its expected shape."""


@dataclass(slots=True)
class _HtmlTableCell:
    """Text captured from one filing-detail table cell."""

    text: list[str]
    link_text: list[str]


class _FilingDocumentTableParser(HTMLParser):
    """Capture rows from the SEC's Document Format Files table only."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[tuple[_HtmlTableCell, ...]] = []
        self.target_table_count = 0
        self.target_table_closed = False
        self._table_depth = 0
        self._target_depth: int | None = None
        self._current_row: list[_HtmlTableCell] | None = None
        self._current_cell: _HtmlTableCell | None = None
        self._anchor_depth = 0

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        normalized_tag = tag.casefold()
        if normalized_tag == "table":
            self._table_depth += 1
            attributes = {name.casefold(): value for name, value in attrs}
            summary = attributes.get("summary")
            if summary is not None and summary.strip().casefold() == (
                "document format files"
            ):
                self.target_table_count += 1
                if self._target_depth is None:
                    self._target_depth = self._table_depth
            return
        if self._target_depth is None:
            return
        if normalized_tag == "tr":
            self._current_row = []
            self._current_cell = None
        elif normalized_tag == "td" and self._current_row is not None:
            self._current_cell = _HtmlTableCell(text=[], link_text=[])
        elif normalized_tag == "a" and self._current_cell is not None:
            self._anchor_depth += 1

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.casefold()
        if self._target_depth is not None:
            if normalized_tag == "a" and self._anchor_depth:
                self._anchor_depth -= 1
            elif normalized_tag == "td" and self._current_cell is not None:
                if self._current_row is not None:
                    self._current_row.append(self._current_cell)
                self._current_cell = None
                self._anchor_depth = 0
            elif normalized_tag == "tr" and self._current_row is not None:
                if self._current_row:
                    self.rows.append(tuple(self._current_row))
                self._current_row = None
                self._current_cell = None
                self._anchor_depth = 0
        if normalized_tag == "table":
            if self._target_depth == self._table_depth:
                self.target_table_closed = True
                self._target_depth = None
                self._current_row = None
                self._current_cell = None
                self._anchor_depth = 0
            self._table_depth = max(0, self._table_depth - 1)

    def handle_data(self, data: str) -> None:
        if self._current_cell is None:
            return
        self._current_cell.text.append(data)
        if self._anchor_depth:
            self._current_cell.link_text.append(data)


def parse_filing_documents(
    body: bytes,
    *,
    cik: str,
    accession_number: str,
) -> tuple[SecFilingDocument, ...]:
    """Parse and validate an SEC Document Format Files table."""
    parser = _FilingDocumentTableParser()
    parser.feed(body.decode("utf-8", errors="replace"))
    parser.close()
    if parser.target_table_count != 1 or not parser.target_table_closed:
        raise SecFilingDocumentResponseError(
            "filing detail must contain exactly one Document Format Files table"
        )

    documents: list[SecFilingDocument] = []
    sequences: set[int] = set()
    document_names: set[str] = set()
    for row_number, cells in enumerate(parser.rows, start=1):
        if len(cells) != 5:
            raise SecFilingDocumentResponseError(
                f"filing document row {row_number} must contain five columns"
            )
        sequence_text = _normalized_cell_text(cells[0].text)
        description = _normalized_cell_text(cells[1].text)
        if not sequence_text and description.casefold() == (
            "complete submission text file"
        ):
            continue
        sequence = _positive_integer(
            sequence_text,
            context=f"sequence in filing document row {row_number}",
        )
        document_name = _normalized_cell_text(cells[2].link_text)
        document_type = _normalized_cell_text(cells[3].text)
        size_bytes = _nonnegative_integer(
            _normalized_cell_text(cells[4].text),
            context=f"size in filing document row {row_number}",
        )
        if not document_name:
            raise SecFilingDocumentResponseError(
                f"document name is empty in filing document row {row_number}"
            )
        if len(document_name) > 512:
            raise SecFilingDocumentResponseError(
                f"document name is too long in filing document row {row_number}"
            )
        if not document_type:
            raise SecFilingDocumentResponseError(
                f"document type is empty in filing document row {row_number}"
            )
        if len(description) > 1000:
            raise SecFilingDocumentResponseError(
                f"description is too long in filing document row {row_number}"
            )
        if len(document_type) > 100:
            raise SecFilingDocumentResponseError(
                f"document type is too long in filing document row {row_number}"
            )
        try:
            source_url = sec_filing_document_url(
                cik,
                accession_number,
                document_name,
            )
        except ValueError as error:
            raise SecFilingDocumentResponseError(
                f"invalid document name in filing document row {row_number}: "
                f"{document_name!r}"
            ) from error
        if sequence in sequences:
            raise SecFilingDocumentResponseError(
                f"duplicate filing document sequence: {sequence}"
            )
        if document_name in document_names:
            raise SecFilingDocumentResponseError(
                f"duplicate filing document name: {document_name!r}"
            )
        sequences.add(sequence)
        document_names.add(document_name)
        documents.append(
            SecFilingDocument(
                sequence=sequence,
                description=description or None,
                document_name=document_name,
                document_type=document_type,
                size_bytes=size_bytes,
                source_url=source_url,
            )
        )
    if not documents:
        raise SecFilingDocumentResponseError(
            "filing detail contains no document entries"
        )
    return tuple(documents)


def _normalized_cell_text(parts: list[str]) -> str:
    return " ".join(" ".join(parts).split())


def _positive_integer(value: str, *, context: str) -> int:
    parsed = _nonnegative_integer(value, context=context)
    if parsed < 1:
        raise SecFilingDocumentResponseError(f"{context} must be positive: {value!r}")
    return parsed


def _nonnegative_integer(value: str, *, context: str) -> int:
    normalized = value.replace(",", "")
    if not normalized.isascii() or not normalized.isdigit():
        raise SecFilingDocumentResponseError(
            f"{context} is not a non-negative integer: {value!r}"
        )
    return int(normalized)
