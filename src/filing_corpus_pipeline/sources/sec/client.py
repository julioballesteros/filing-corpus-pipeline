"""Typed client for the SEC EDGAR endpoints used by the pipeline."""

import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime

from filing_corpus_pipeline.sources.http import (
    HttpTransport,
    HttpTransportError,
)
from filing_corpus_pipeline.sources.sec.identifiers import (
    normalize_cik,
    sec_primary_document_url,
)

SEC_DATA_BASE_URL = "https://data.sec.gov/submissions"
SUBMISSION_FILE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class SecRequestError(RuntimeError):
    """A classified failure while calling an SEC EDGAR endpoint."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        code: str = "SEC_REQUEST_FAILED",
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.code = code


class SecResponseError(RuntimeError):
    """An SEC response that violates its documented shape."""


@dataclass(frozen=True, slots=True)
class SecEdgarClientConfig:
    """Identity, timeout, pacing, and metadata retry policy for SEC requests."""

    user_agent: str
    timeout_seconds: float = 10.0
    metadata_max_attempts: int = 3
    metadata_initial_backoff_seconds: float = 0.25
    request_interval_seconds: float = 0.125

    def __post_init__(self) -> None:
        if not self.user_agent.strip():
            raise ValueError("SEC user_agent must not be empty")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.metadata_max_attempts < 1:
            raise ValueError("metadata_max_attempts must be at least one")
        if self.metadata_initial_backoff_seconds < 0:
            raise ValueError("metadata_initial_backoff_seconds must not be negative")
        if self.request_interval_seconds < 0:
            raise ValueError("request_interval_seconds must not be negative")


@dataclass(frozen=True, slots=True)
class SecSubmission:
    """One typed row returned by an SEC submissions endpoint."""

    cik: str
    issuer_name: str
    accession_number: str
    form: str
    filed_on: date
    report_date: date | None
    accepted_at: datetime | None
    primary_document: str


@dataclass(frozen=True, slots=True)
class SecSubmissionFile:
    """One historical submissions-file descriptor."""

    name: str
    filing_from: date
    filing_to: date


@dataclass(frozen=True, slots=True)
class SecCompanySubmissions:
    """Recent filings and available history for one SEC registrant."""

    cik: str
    issuer_name: str
    recent: tuple[SecSubmission, ...]
    files: tuple[SecSubmissionFile, ...]


@dataclass(frozen=True, slots=True)
class SecDocument:
    """A bounded SEC filing document with source response metadata."""

    body: bytes
    content_type: str
    source_url: str
    etag: str | None
    last_modified: str | None


class SecEdgarClient:
    """Expose typed operations over SEC submissions and filing archives."""

    def __init__(
        self,
        *,
        config: SecEdgarClientConfig,
        transport: HttpTransport,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._config = config
        self._transport = transport
        self._sleep = sleep
        self._request_started = False

    def get_company_submissions(self, cik: str) -> SecCompanySubmissions:
        """Retrieve recent submissions and history descriptors for one CIK."""
        normalized_cik = normalize_cik(cik)
        payload = _as_mapping(
            self._get_json(f"CIK{normalized_cik}.json"),
            context=f"submissions for CIK {normalized_cik}",
        )
        issuer_name = _required_string(payload, "name")
        filings = _required_mapping(payload, "filings")
        recent = _parse_submission_rows(
            _required_mapping(filings, "recent"),
            cik=normalized_cik,
            issuer_name=issuer_name,
        )
        files = tuple(
            _parse_submission_file(value)
            for value in _as_list(filings.get("files", []), context="filings.files")
        )
        return SecCompanySubmissions(
            cik=normalized_cik,
            issuer_name=issuer_name,
            recent=recent,
            files=files,
        )

    def get_historical_submissions(
        self,
        file_name: str,
        *,
        cik: str,
        issuer_name: str,
    ) -> tuple[SecSubmission, ...]:
        """Retrieve one history file already advertised by the company index."""
        if SUBMISSION_FILE_PATTERN.fullmatch(file_name) is None:
            raise ValueError("invalid SEC submissions history file name")
        if not issuer_name.strip():
            raise ValueError("issuer_name must not be empty")
        normalized_cik = normalize_cik(cik)
        payload = _as_mapping(
            self._get_json(file_name),
            context=f"historical submissions file {file_name}",
        )
        return _parse_submission_rows(
            payload,
            cik=normalized_cik,
            issuer_name=issuer_name,
        )

    def get_filing_document(
        self,
        *,
        cik: str,
        accession_number: str,
        primary_document: str,
        max_bytes: int,
    ) -> SecDocument:
        """Retrieve one bounded primary document from the SEC archive."""
        url = sec_primary_document_url(cik, accession_number, primary_document)
        self._pace_request()
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
                max_bytes=max_bytes,
            )
        except HttpTransportError as error:
            raise SecRequestError(
                str(error),
                retryable=error.retryable,
                code=error.code,
            ) from error
        return SecDocument(
            body=response.body,
            content_type=response.content_type,
            source_url=url,
            etag=response.etag,
            last_modified=response.last_modified,
        )

    def _get_json(self, file_name: str) -> object:
        url = f"{SEC_DATA_BASE_URL}/{file_name}"
        headers = {
            "Accept": "application/json",
            "User-Agent": self._config.user_agent,
        }
        for attempt in range(1, self._config.metadata_max_attempts + 1):
            self._pace_request()
            try:
                return self._transport.get_json(
                    url,
                    headers=headers,
                    timeout_seconds=self._config.timeout_seconds,
                )
            except HttpTransportError as error:
                if not error.retryable or attempt == self._config.metadata_max_attempts:
                    raise SecRequestError(
                        str(error),
                        retryable=error.retryable,
                        code=error.code,
                    ) from error
                backoff = self._config.metadata_initial_backoff_seconds * 2 ** (
                    attempt - 1
                )
                self._sleep(backoff)
        raise AssertionError("retry loop completed without returning or raising")

    def _pace_request(self) -> None:
        if self._request_started and self._config.request_interval_seconds:
            self._sleep(self._config.request_interval_seconds)
        self._request_started = True


def _parse_submission_file(value: object) -> SecSubmissionFile:
    descriptor = _as_mapping(value, context="filings.files entry")
    name = _required_string(descriptor, "name")
    if SUBMISSION_FILE_PATTERN.fullmatch(name) is None:
        raise SecResponseError(f"invalid submissions history file name: {name!r}")
    filing_from = _required_date(descriptor, "filingFrom")
    filing_to = _required_date(descriptor, "filingTo")
    if filing_from > filing_to:
        raise SecResponseError(f"history file {name!r} has filingFrom after filingTo")
    return SecSubmissionFile(
        name=name,
        filing_from=filing_from,
        filing_to=filing_to,
    )


def _parse_submission_rows(
    payload: Mapping[str, object],
    *,
    cik: str,
    issuer_name: str,
) -> tuple[SecSubmission, ...]:
    column_names = (
        "accessionNumber",
        "form",
        "filingDate",
        "reportDate",
        "acceptanceDateTime",
        "primaryDocument",
    )
    columns = {name: _string_list(payload, name) for name in column_names}
    row_count = len(columns["accessionNumber"])
    lengths = {name: len(values) for name, values in columns.items()}
    if any(length != row_count for length in lengths.values()):
        raise SecResponseError(f"submission columns have unequal lengths: {lengths}")

    rows: list[SecSubmission] = []
    for index in range(row_count):
        accession_number = columns["accessionNumber"][index]
        if re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession_number) is None:
            raise SecResponseError(
                f"invalid accession number at row {index}: {accession_number!r}"
            )
        primary_document = columns["primaryDocument"][index]
        if not primary_document:
            raise SecResponseError(f"primaryDocument is empty at row {index}")

        rows.append(
            SecSubmission(
                cik=cik,
                issuer_name=issuer_name,
                accession_number=accession_number,
                form=columns["form"][index],
                filed_on=_parse_date(
                    columns["filingDate"][index],
                    context=f"filingDate at row {index}",
                ),
                report_date=_parse_optional_date(
                    columns["reportDate"][index],
                    context=f"reportDate at row {index}",
                ),
                accepted_at=_parse_optional_datetime(
                    columns["acceptanceDateTime"][index],
                    context=f"acceptanceDateTime at row {index}",
                ),
                primary_document=primary_document,
            )
        )
    return tuple(rows)


def _as_mapping(value: object, *, context: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise SecResponseError(f"{context} must be a JSON object")
    return value


def _as_list(value: object, *, context: str) -> list[object]:
    if not isinstance(value, list):
        raise SecResponseError(f"{context} must be a JSON array")
    return value


def _required_mapping(
    payload: Mapping[str, object],
    key: str,
) -> Mapping[str, object]:
    if key not in payload:
        raise SecResponseError(f"missing required object: {key}")
    return _as_mapping(payload[key], context=key)


def _required_string(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise SecResponseError(f"{key} must be a non-empty string")
    return value


def _string_list(payload: Mapping[str, object], key: str) -> list[str]:
    if key not in payload:
        raise SecResponseError(f"missing required column: {key}")
    values = _as_list(payload[key], context=key)
    if not all(isinstance(value, str) for value in values):
        raise SecResponseError(f"{key} must contain only strings")
    return values  # type: ignore[return-value]


def _required_date(payload: Mapping[str, object], key: str) -> date:
    return _parse_date(_required_string(payload, key), context=key)


def _parse_date(value: str, *, context: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise SecResponseError(f"{context} is not an ISO date: {value!r}") from error


def _parse_optional_date(value: str, *, context: str) -> date | None:
    return _parse_date(value, context=context) if value else None


def _parse_optional_datetime(value: str, *, context: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise SecResponseError(
            f"{context} is not an ISO datetime: {value!r}"
        ) from error
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
