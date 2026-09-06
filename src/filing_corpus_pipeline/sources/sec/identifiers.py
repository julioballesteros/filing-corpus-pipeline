"""Validation and canonical URL construction for SEC identities."""

import re

SEC_ARCHIVE_BASE_URL = "https://www.sec.gov/Archives/edgar/data"
ACCESSION_PATTERN = re.compile(r"\d{10}-\d{2}-\d{6}")
DOCUMENT_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def normalize_cik(value: str) -> str:
    """Return the ten-digit CIK form required by SEC data endpoints."""
    stripped = value.strip()
    if not stripped.isascii() or not stripped.isdigit() or len(stripped) > 10:
        raise ValueError(f"invalid SEC CIK: {value!r}")
    return stripped.zfill(10)


def sec_filing_directory_url(cik: str, accession_number: str) -> str:
    """Build the canonical archive directory for one SEC filing."""
    normalized_cik = normalize_cik(cik)
    if ACCESSION_PATTERN.fullmatch(accession_number) is None:
        raise ValueError(f"invalid SEC accession number: {accession_number!r}")
    archive_cik = str(int(normalized_cik))
    accession_path = accession_number.replace("-", "")
    return f"{SEC_ARCHIVE_BASE_URL}/{archive_cik}/{accession_path}"


def sec_filing_detail_url(cik: str, accession_number: str) -> str:
    """Build the canonical filing-detail URL for one SEC submission."""
    directory_url = sec_filing_directory_url(cik, accession_number)
    return f"{directory_url}/{accession_number}-index.html"


def sec_filing_document_url(
    cik: str,
    accession_number: str,
    document_name: str,
) -> str:
    """Build the canonical archive URL for one document in a filing."""
    if DOCUMENT_NAME_PATTERN.fullmatch(document_name) is None:
        raise ValueError(f"invalid SEC filing document name: {document_name!r}")
    return f"{sec_filing_directory_url(cik, accession_number)}/{document_name}"


def sec_primary_document_url(
    cik: str,
    accession_number: str,
    primary_document: str,
) -> str:
    """Build the canonical URL for a filing's primary document."""
    return sec_filing_document_url(cik, accession_number, primary_document)
