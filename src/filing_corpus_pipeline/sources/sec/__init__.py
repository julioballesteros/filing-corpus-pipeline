"""Shared SEC EDGAR API client and identifier utilities."""

from filing_corpus_pipeline.sources.sec.client import (
    SEC_DATA_BASE_URL,
    SecCompanySubmissions,
    SecDocument,
    SecEdgarClient,
    SecEdgarClientConfig,
    SecRequestError,
    SecResponseError,
    SecSubmission,
    SecSubmissionFile,
)
from filing_corpus_pipeline.sources.sec.filing_documents import SecFilingDocument
from filing_corpus_pipeline.sources.sec.identifiers import (
    SEC_ARCHIVE_BASE_URL,
    normalize_cik,
    sec_filing_detail_url,
    sec_filing_directory_url,
    sec_filing_document_url,
    sec_primary_document_url,
)

__all__ = [
    "SEC_ARCHIVE_BASE_URL",
    "SEC_DATA_BASE_URL",
    "SecCompanySubmissions",
    "SecDocument",
    "SecEdgarClient",
    "SecEdgarClientConfig",
    "SecFilingDocument",
    "SecRequestError",
    "SecResponseError",
    "SecSubmission",
    "SecSubmissionFile",
    "normalize_cik",
    "sec_filing_detail_url",
    "sec_filing_directory_url",
    "sec_filing_document_url",
    "sec_primary_document_url",
]
