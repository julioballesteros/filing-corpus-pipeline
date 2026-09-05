"""SEC-specific primary-document acquisition behavior."""

from dataclasses import dataclass

from filing_corpus_pipeline.acquisition.models import (
    DocumentRetrievalError,
    RetrievedDocument,
)
from filing_corpus_pipeline.domain import FilingReference
from filing_corpus_pipeline.sources.sec import (
    SecEdgarClient,
    SecRequestError,
    normalize_cik,
    sec_primary_document_url,
)


@dataclass(frozen=True, slots=True)
class SecDocumentConfig:
    """Resource bound for one SEC primary-document request."""

    max_document_bytes: int = 25 * 1024 * 1024

    def __post_init__(self) -> None:
        if self.max_document_bytes < 1:
            raise ValueError("max_document_bytes must be positive")


class SecFilingDocumentSource:
    """Retrieve the canonical primary document for an SEC filing reference."""

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
        """Validate SEC identity fields and retrieve the bounded source bytes."""
        url = _canonical_document_url(filing)
        if filing.primary_document_url != url:
            raise DocumentRetrievalError(
                "SEC primary document URL does not match the filing identity",
                code="SEC_DOCUMENT_URL_MISMATCH",
                retryable=False,
            )
        try:
            document = self._client.get_filing_document(
                cik=filing.issuer.provider_issuer_id,
                accession_number=filing.provider_filing_id,
                primary_document=filing.primary_document,
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
        return RetrievedDocument(
            body=document.body,
            content_type=document.content_type,
            source_url=document.source_url,
            source_etag=document.etag,
            source_last_modified=document.last_modified,
        )


def _canonical_document_url(filing: FilingReference) -> str:
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
        return sec_primary_document_url(
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
