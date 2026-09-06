"""SEC-specific document selection and acquisition behavior."""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from filing_corpus_pipeline.acquisition.models import (
    DocumentRetrievalError,
    RetrievedDocument,
)
from filing_corpus_pipeline.domain import (
    DocumentPolicy,
    FilingReference,
    SourceDocumentReference,
)
from filing_corpus_pipeline.sources.sec import (
    SecDocument,
    SecEdgarClient,
    SecFilingDocument,
    SecRequestError,
    SecResponseError,
    normalize_cik,
    sec_filing_detail_url,
    sec_primary_document_url,
)

SEC_PRIMARY_RESOLVER_VERSION = "sec-primary-v1"
SEC_EARNINGS_RELEASE_RESOLVER_VERSION = "sec-earnings-release-v1"
_EXHIBIT_99_PATTERN = re.compile(r"EX-99(?:\.(\d+))?", re.IGNORECASE)
_EARNINGS_DESCRIPTION_PHRASES = (
    "earnings release",
    "earnings results",
    "financial results",
    "quarterly results",
    "quarter results",
    "annual results",
    "results of operations",
)
_RESULT_CONTEXT_TOKENS = frozenset(
    {"financial", "quarter", "quarterly", "annual", "fiscal", "operations"}
)
_RELEASE_DESCRIPTION_PHRASES = ("press release", "news release")


@dataclass(frozen=True, slots=True)
class SecDocumentConfig:
    """Resource bounds for SEC filing metadata and document requests."""

    max_document_bytes: int = 25 * 1024 * 1024
    max_filing_detail_bytes: int = 2 * 1024 * 1024

    def __post_init__(self) -> None:
        if self.max_document_bytes < 1:
            raise ValueError("max_document_bytes must be positive")
        if self.max_filing_detail_bytes < 1:
            raise ValueError("max_filing_detail_bytes must be positive")


class SecFilingDocumentSource:
    """Resolve and retrieve the selected document for an SEC filing."""

    provider = "sec"

    def __init__(
        self,
        *,
        client: SecEdgarClient,
        config: SecDocumentConfig,
    ) -> None:
        self._client = client
        self._config = config

    def retrieve(self, filing: FilingReference) -> RetrievedDocument:
        """Validate identity, resolve policy, and retrieve bounded source bytes."""
        normalized_cik = _validated_filing_identity(filing)
        if filing.document_policy is DocumentPolicy.PRIMARY:
            source_document = SourceDocumentReference(
                document_name=filing.primary_document,
                provider_document_type=filing.filing_type,
                description=None,
                source_url=filing.primary_document_url,
                resolver_version=SEC_PRIMARY_RESOLVER_VERSION,
            )
        elif filing.document_policy is DocumentPolicy.EARNINGS_RELEASE:
            source_document = self._resolve_earnings_release(
                filing,
                normalized_cik=normalized_cik,
            )
        else:
            raise DocumentRetrievalError(
                f"unsupported SEC document policy: {filing.document_policy!r}",
                code="SEC_UNSUPPORTED_DOCUMENT_POLICY",
                retryable=False,
            )
        document = self._retrieve_document(
            filing,
            normalized_cik=normalized_cik,
            source_document=source_document,
        )
        return RetrievedDocument(
            source_document=source_document,
            body=document.body,
            content_type=document.content_type,
            source_etag=document.etag,
            source_last_modified=document.last_modified,
        )

    def _resolve_earnings_release(
        self,
        filing: FilingReference,
        *,
        normalized_cik: str,
    ) -> SourceDocumentReference:
        if filing.filing_type != "8-K":
            raise DocumentRetrievalError(
                "SEC earnings-release policy requires an 8-K filing",
                code="SEC_EARNINGS_RELEASE_REQUIRES_8_K",
                retryable=False,
            )
        try:
            documents = self._client.get_filing_documents(
                cik=normalized_cik,
                accession_number=filing.provider_filing_id,
                max_bytes=self._config.max_filing_detail_bytes,
            )
        except SecRequestError as error:
            code = (
                "SEC_FILING_DETAIL_TOO_LARGE"
                if error.code == "RESPONSE_TOO_LARGE"
                else "SEC_FILING_DETAIL_HTTP_ERROR"
            )
            raise DocumentRetrievalError(
                str(error),
                code=code,
                retryable=error.retryable,
            ) from error
        except SecResponseError as error:
            raise DocumentRetrievalError(
                str(error),
                code="SEC_INVALID_FILING_DETAIL",
                retryable=False,
            ) from error

        selected = resolve_sec_earnings_release_document(documents)
        if selected.size_bytes > self._config.max_document_bytes:
            raise DocumentRetrievalError(
                "selected SEC earnings release exceeds the document byte limit",
                code="SEC_DOCUMENT_TOO_LARGE",
                retryable=False,
            )
        return SourceDocumentReference(
            document_name=selected.document_name,
            provider_document_type=selected.document_type,
            description=selected.description,
            source_url=selected.source_url,
            resolver_version=SEC_EARNINGS_RELEASE_RESOLVER_VERSION,
        )

    def _retrieve_document(
        self,
        filing: FilingReference,
        *,
        normalized_cik: str,
        source_document: SourceDocumentReference,
    ) -> SecDocument:
        try:
            document = self._client.get_filing_document(
                cik=normalized_cik,
                accession_number=filing.provider_filing_id,
                document_name=source_document.document_name,
                max_bytes=self._config.max_document_bytes,
            )
        except SecRequestError as error:
            code = (
                "SEC_DOCUMENT_TOO_LARGE"
                if error.code == "RESPONSE_TOO_LARGE"
                else "SEC_DOCUMENT_HTTP_ERROR"
            )
            raise DocumentRetrievalError(
                str(error),
                code=code,
                retryable=error.retryable,
            ) from error
        if not document.body:
            raise DocumentRetrievalError(
                "SEC returned an empty filing document",
                code="SEC_EMPTY_DOCUMENT",
                retryable=False,
            )
        if document.source_url != source_document.source_url:
            raise DocumentRetrievalError(
                "SEC document response URL does not match its resolved identity",
                code="SEC_DOCUMENT_URL_MISMATCH",
                retryable=False,
            )
        return document


def resolve_sec_earnings_release_document(
    documents: Sequence[SecFilingDocument],
) -> SecFilingDocument:
    """Select one earnings exhibit conservatively from an SEC document list."""
    exhibits = tuple(
        document
        for document in documents
        if _EXHIBIT_99_PATTERN.fullmatch(document.document_type.strip()) is not None
    )
    if not exhibits:
        raise DocumentRetrievalError(
            "SEC filing contains no EX-99 document",
            code="SEC_EARNINGS_RELEASE_NOT_FOUND",
            retryable=False,
        )

    earnings_candidates = tuple(
        document for document in exhibits if _is_earnings_description(document)
    )
    if earnings_candidates:
        return _select_unambiguous_earnings_candidate(earnings_candidates)
    release_candidates = tuple(
        document
        for document in exhibits
        if _description_contains(document, _RELEASE_DESCRIPTION_PHRASES)
    )
    if release_candidates:
        return _select_unambiguous_earnings_candidate(release_candidates)
    return _select_unambiguous_earnings_candidate(exhibits)


def _select_unambiguous_earnings_candidate(
    candidates: Sequence[SecFilingDocument],
) -> SecFilingDocument:
    if len(candidates) == 1:
        return candidates[0]
    exhibit_99_1 = tuple(
        document
        for document in candidates
        if _exhibit_number(document.document_type) == 1
    )
    if len(exhibit_99_1) == 1:
        return exhibit_99_1[0]
    raise DocumentRetrievalError(
        f"SEC filing has {len(candidates)} ambiguous earnings-release candidates",
        code="SEC_EARNINGS_RELEASE_AMBIGUOUS",
        retryable=False,
    )


def _description_contains(
    document: SecFilingDocument,
    phrases: Sequence[str],
) -> bool:
    if document.description is None:
        return False
    normalized = re.sub(r"[^a-z0-9]+", " ", document.description.casefold()).strip()
    return any(phrase in normalized for phrase in phrases)


def _is_earnings_description(document: SecFilingDocument) -> bool:
    if document.description is None:
        return False
    normalized = re.sub(r"[^a-z0-9]+", " ", document.description.casefold()).strip()
    if any(phrase in normalized for phrase in _EARNINGS_DESCRIPTION_PHRASES):
        return True
    tokens = frozenset(normalized.split())
    return "results" in tokens and bool(tokens & _RESULT_CONTEXT_TOKENS)


def _exhibit_number(document_type: str) -> int | None:
    match = _EXHIBIT_99_PATTERN.fullmatch(document_type.strip())
    if match is None or match.group(1) is None:
        return None
    return int(match.group(1))


def _validated_filing_identity(filing: FilingReference) -> str:
    if filing.provider != "sec":
        raise DocumentRetrievalError(
            f"SEC source cannot retrieve provider {filing.provider!r}",
            code="SEC_PROVIDER_MISMATCH",
            retryable=False,
        )
    try:
        normalized_cik = normalize_cik(filing.issuer.provider_issuer_id)
    except ValueError as error:
        raise DocumentRetrievalError(
            "SEC issuer ID is not a valid CIK",
            code="SEC_INVALID_CIK",
            retryable=False,
        ) from error
    try:
        detail_url = sec_filing_detail_url(
            normalized_cik,
            filing.provider_filing_id,
        )
        primary_document_url = sec_primary_document_url(
            normalized_cik,
            filing.provider_filing_id,
            filing.primary_document,
        )
    except ValueError as error:
        message = str(error)
        code = (
            "SEC_INVALID_ACCESSION"
            if "accession" in message
            else "SEC_INVALID_DOCUMENT_NAME"
        )
        raise DocumentRetrievalError(
            message,
            code=code,
            retryable=False,
        ) from error
    if filing.filing_detail_url != detail_url:
        raise DocumentRetrievalError(
            "SEC filing-detail URL does not match the filing identity",
            code="SEC_FILING_DETAIL_URL_MISMATCH",
            retryable=False,
        )
    if filing.primary_document_url != primary_document_url:
        raise DocumentRetrievalError(
            "SEC primary document URL does not match the filing identity",
            code="SEC_DOCUMENT_URL_MISMATCH",
            retryable=False,
        )
    return normalized_cik
