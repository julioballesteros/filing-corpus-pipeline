"""Retrieve bounded primary filing documents from the SEC archive."""

import re
from dataclasses import dataclass

from filing_corpus_pipeline.acquisition.models import (
    DocumentRetrievalError,
    RetrievedDocument,
)
from filing_corpus_pipeline.adapters.http import BytesHttpTransport, HttpTransportError
from filing_corpus_pipeline.adapters.sec.discovery import SEC_ARCHIVE_BASE_URL
from filing_corpus_pipeline.adapters.sec.submissions import normalize_cik
from filing_corpus_pipeline.domain import FilingReference

ACCESSION_PATTERN = re.compile(r"\d{10}-\d{2}-\d{6}")
DOCUMENT_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


@dataclass(frozen=True, slots=True)
class SecDocumentConfig:
    """Resource and identity policy for one SEC document request."""

    user_agent: str
    timeout_seconds: float = 20.0
    max_document_bytes: int = 25 * 1024 * 1024

    def __post_init__(self) -> None:
        if not self.user_agent.strip():
            raise ValueError("SEC user_agent must not be empty")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.max_document_bytes < 1:
            raise ValueError("max_document_bytes must be positive")


class SecFilingDocumentSource:
    """Retrieve the canonical primary document for an SEC filing reference."""

    provider = "sec"

    def __init__(
        self,
        *,
        transport: BytesHttpTransport,
        config: SecDocumentConfig,
    ) -> None:
        self._transport = transport
        self._config = config

    def retrieve(self, filing: FilingReference) -> RetrievedDocument:
        """Validate SEC identity fields and retrieve the bounded document body."""
        url = _canonical_document_url(filing)
        if filing.primary_document_url != url:
            raise DocumentRetrievalError(
                "SEC primary document URL does not match the filing identity",
                code="SEC_DOCUMENT_URL_MISMATCH",
                retryable=False,
            )
        try:
            response = self._transport.get_bytes(
                url,
                headers={
                    "Accept": (
                        "text/html, application/xhtml+xml, "
                        "application/xml;q=0.9, */*;q=0.1"
                    ),
                    "User-Agent": self._config.user_agent,
                },
                timeout_seconds=self._config.timeout_seconds,
                max_bytes=self._config.max_document_bytes,
            )
        except HttpTransportError as error:
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
        if not response.body:
            raise DocumentRetrievalError(
                "SEC returned an empty filing document",
                code="SEC_EMPTY_DOCUMENT",
                retryable=False,
            )
        return RetrievedDocument(
            body=response.body,
            content_type=response.content_type,
            source_url=url,
            source_etag=response.etag,
            source_last_modified=response.last_modified,
        )


def _canonical_document_url(filing: FilingReference) -> str:
    if filing.provider != "sec":
        raise DocumentRetrievalError(
            f"SEC adapter cannot retrieve provider {filing.provider!r}",
            code="SEC_PROVIDER_MISMATCH",
            retryable=False,
        )
    if ACCESSION_PATTERN.fullmatch(filing.provider_filing_id) is None:
        raise DocumentRetrievalError(
            "SEC filing ID is not a valid accession number",
            code="SEC_INVALID_ACCESSION",
            retryable=False,
        )
    if DOCUMENT_NAME_PATTERN.fullmatch(filing.primary_document) is None:
        raise DocumentRetrievalError(
            "SEC primary document name contains unsupported characters",
            code="SEC_INVALID_DOCUMENT_NAME",
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
    archive_cik = str(int(normalized_cik))
    accession_path = filing.provider_filing_id.replace("-", "")
    return (
        f"{SEC_ARCHIVE_BASE_URL}/{archive_cik}/{accession_path}/"
        f"{filing.primary_document}"
    )
