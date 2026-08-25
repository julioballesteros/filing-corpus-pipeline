"""Client and parser for the SEC EDGAR submissions endpoint."""

import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime

from filing_corpus_pipeline.adapters.http import (
    HttpTransportError,
    JsonHttpTransport,
)
from filing_corpus_pipeline.adapters.sec.identifiers import normalize_cik

SEC_DATA_BASE_URL = "https://data.sec.gov/submissions"


class SecRequestError(RuntimeError):
    """Raised when an SEC request exhausts its retry policy."""


class SecResponseError(RuntimeError):
    """Raised when an SEC response violates its documented columnar shape."""


@dataclass(frozen=True, slots=True)
class SecClientConfig:
    """Runtime policy for accessing the public SEC endpoint."""

    user_agent: str
    timeout_seconds: float = 10.0
    max_attempts: int = 3
    initial_backoff_seconds: float = 0.25
    request_interval_seconds: float = 0.125

    def __post_init__(self) -> None:
        if not self.user_agent.strip():
            raise ValueError("SEC user_agent must not be empty")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        if self.initial_backoff_seconds < 0:
            raise ValueError("initial_backoff_seconds must not be negative")
        if self.request_interval_seconds < 0:
            raise ValueError("request_interval_seconds must not be negative")


@dataclass(frozen=True, slots=True)
class SecSubmission:
    """One row from an SEC submissions response."""

    cik: str
    issuer_name: str
    accession_number: str
    form: str
    filed_on: date
    report_date: date | None
    accepted_at: datetime | None
    primary_document: str


class SecSubmissionsClient:
    """Retrieve current and overlapping historical submission metadata."""

    def __init__(
        self,
        *,
        transport: JsonHttpTransport,
        config: SecClientConfig,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._transport = transport
        self._config = config
        self._sleep = sleep
        self._request_started = False

    def list_submissions(
        self,
        cik: str,
        *,
        filed_from: date,
        filed_to: date,
    ) -> list[SecSubmission]:
        """Return recent rows plus historical files overlapping the date range."""
        normalized_cik = normalize_cik(cik)
        main_payload = _as_mapping(
            self._get_json(f"CIK{normalized_cik}.json"),
            context=f"submissions for CIK {normalized_cik}",
        )
        issuer_name = _required_string(main_payload, "name")
        filings = _required_mapping(main_payload, "filings")
        recent = _required_mapping(filings, "recent")

        submissions = _parse_submission_rows(
            recent,
            cik=normalized_cik,
            issuer_name=issuer_name,
        )

        files_value = filings.get("files", [])
        for descriptor_value in _as_list(files_value, context="filings.files"):
            descriptor = _as_mapping(
                descriptor_value,
                context="filings.files entry",
            )
            history_from = _required_date(descriptor, "filingFrom")
            history_to = _required_date(descriptor, "filingTo")
            if history_to < filed_from or history_from > filed_to:
                continue

            file_name = _required_string(descriptor, "name")
            history_payload = _as_mapping(
                self._get_json(file_name),
                context=f"historical submissions file {file_name}",
            )
            submissions.extend(
                _parse_submission_rows(
                    history_payload,
                    cik=normalized_cik,
                    issuer_name=issuer_name,
                )
            )

        return submissions

    def _get_json(self, file_name: str) -> object:
        url = f"{SEC_DATA_BASE_URL}/{file_name}"
        headers = {
            "Accept": "application/json",
            "User-Agent": self._config.user_agent,
        }
        for attempt in range(1, self._config.max_attempts + 1):
            if self._request_started and self._config.request_interval_seconds:
                self._sleep(self._config.request_interval_seconds)
            self._request_started = True
            try:
                return self._transport.get_json(
                    url,
                    headers=headers,
                    timeout_seconds=self._config.timeout_seconds,
                )
            except HttpTransportError as error:
                if not error.retryable or attempt == self._config.max_attempts:
                    raise SecRequestError(str(error)) from error
                backoff = self._config.initial_backoff_seconds * 2 ** (attempt - 1)
                self._sleep(backoff)

        raise AssertionError("retry loop completed without returning or raising")


def _parse_submission_rows(
    payload: Mapping[str, object],
    *,
    cik: str,
    issuer_name: str,
) -> list[SecSubmission]:
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
    return rows


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
