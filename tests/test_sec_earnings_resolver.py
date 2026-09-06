"""Tests for deterministic SEC earnings-release exhibit selection."""

import pytest

from filing_corpus_pipeline.acquisition import DocumentRetrievalError
from filing_corpus_pipeline.acquisition.sec import (
    resolve_sec_earnings_release_document,
)
from filing_corpus_pipeline.sources.sec import SecFilingDocument


def document(
    *,
    sequence: int,
    document_type: str,
    description: str | None,
    name: str | None = None,
) -> SecFilingDocument:
    """Build one already-validated SEC filing document."""
    document_name = name or f"document-{sequence}.htm"
    return SecFilingDocument(
        sequence=sequence,
        description=description,
        document_name=document_name,
        document_type=document_type,
        size_bytes=100,
        source_url=f"https://www.sec.gov/Archives/{document_name}",
    )


def test_resolver_prefers_explicit_results_over_exhibit_number() -> None:
    """Semantic evidence outranks the conventional EX-99.1 position."""
    presentation = document(
        sequence=2,
        document_type="EX-99.1",
        description="Investor presentation",
    )
    earnings = document(
        sequence=3,
        document_type="EX-99.2",
        description="Financial results for the fourth quarter",
    )

    assert resolve_sec_earnings_release_document((presentation, earnings)) is earnings


def test_resolver_does_not_treat_an_earnings_call_as_results() -> None:
    """A call announcement cannot outrank an explicit quarterly-results exhibit."""
    call = document(
        sequence=2,
        document_type="EX-99.1",
        description="Press release announcing quarterly earnings call",
    )
    results = document(
        sequence=3,
        document_type="EX-99.2",
        description="Results for the second fiscal quarter",
    )

    assert resolve_sec_earnings_release_document((call, results)) is results


def test_resolver_prefers_a_release_description_before_numeric_fallback() -> None:
    """A described press release wins over an unlabeled EX-99.1 attachment."""
    unlabeled = document(
        sequence=2,
        document_type="EX-99.1",
        description="Exhibit 99.1",
    )
    release = document(
        sequence=3,
        document_type="EX-99.2",
        description="Company news release",
    )

    assert resolve_sec_earnings_release_document((unlabeled, release)) is release


def test_resolver_uses_unique_exhibit_99_1_as_conservative_fallback() -> None:
    """SEC convention resolves otherwise unlabeled multiple exhibits."""
    expected = document(
        sequence=2,
        document_type="ex-99.01",
        description=None,
    )
    other = document(
        sequence=3,
        document_type="EX-99.2",
        description="Supplemental material",
    )

    assert resolve_sec_earnings_release_document((other, expected)) is expected


def test_resolver_accepts_the_only_exhibit_99_candidate() -> None:
    """One EX-99 document is unambiguous even without a useful description."""
    expected = document(
        sequence=2,
        document_type="EX-99",
        description=None,
    )
    primary = document(
        sequence=1,
        document_type="8-K",
        description="8-K",
    )

    assert resolve_sec_earnings_release_document((primary, expected)) is expected


@pytest.mark.parametrize(
    "documents",
    [
        (
            document(
                sequence=2,
                document_type="EX-99.2",
                description="Quarterly results",
            ),
            document(
                sequence=3,
                document_type="EX-99.3",
                description="Financial results",
            ),
        ),
        (
            document(
                sequence=2,
                document_type="EX-99.1",
                description="Press release",
            ),
            document(
                sequence=3,
                document_type="EX-99.01",
                description="News release",
            ),
        ),
    ],
)
def test_resolver_rejects_ambiguous_best_candidates(
    documents: tuple[SecFilingDocument, ...],
) -> None:
    """Tied evidence is surfaced rather than broken by arbitrary row order."""
    with pytest.raises(DocumentRetrievalError) as raised:
        resolve_sec_earnings_release_document(documents)

    assert raised.value.code == "SEC_EARNINGS_RELEASE_AMBIGUOUS"
    assert raised.value.retryable is False


def test_resolver_requires_an_exhibit_99_document() -> None:
    """The 8-K primary document is never mistaken for its earnings exhibit."""
    documents = (
        document(sequence=1, document_type="8-K", description="8-K"),
        document(
            sequence=2,
            document_type="EX-101.SCH",
            description="XBRL schema",
        ),
    )

    with pytest.raises(DocumentRetrievalError) as raised:
        resolve_sec_earnings_release_document(documents)

    assert raised.value.code == "SEC_EARNINGS_RELEASE_NOT_FOUND"
    assert raised.value.retryable is False
