"""Local command-line entry point for exercising pipeline use cases."""

import argparse
import json
import os
from collections.abc import Sequence
from datetime import date
from typing import cast

from filing_corpus_pipeline.adapters.http import UrllibJsonTransport
from filing_corpus_pipeline.adapters.sec import (
    SecClientConfig,
    SecFilingDiscoverySource,
    SecSubmissionsClient,
)
from filing_corpus_pipeline.discovery import DiscoveryRequest, DiscoveryService
from filing_corpus_pipeline.domain import FilingForm, IssuerReference


def build_sec_discovery_service(user_agent: str) -> DiscoveryService:
    """Compose the SEC adapter and provider-independent discovery service."""
    client = SecSubmissionsClient(
        transport=UrllibJsonTransport(),
        config=SecClientConfig(user_agent=user_agent),
    )
    return DiscoveryService(SecFilingDiscoverySource(client))


def build_parser() -> argparse.ArgumentParser:
    """Build the local CLI contract."""
    parser = argparse.ArgumentParser(prog="filing-corpus-pipeline")
    subparsers = parser.add_subparsers(dest="command", required=True)

    discover = subparsers.add_parser(
        "discover",
        help="discover SEC filing metadata",
    )
    discover.add_argument(
        "--cik",
        action="append",
        required=True,
        help="SEC CIK to inspect; repeat for multiple issuers",
    )
    discover.add_argument(
        "--form",
        action="append",
        choices=[form.value for form in FilingForm],
        help="filing form to include; defaults to 10-K and 10-Q",
    )
    discover.add_argument(
        "--filed-from",
        required=True,
        help="inclusive filing date in YYYY-MM-DD format",
    )
    discover.add_argument(
        "--filed-to",
        default=date.today().isoformat(),
        help="inclusive filing date; defaults to today",
    )
    discover.add_argument(
        "--user-agent",
        default=os.environ.get("SEC_USER_AGENT"),
        help="declared SEC user agent, or set SEC_USER_AGENT",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """Execute a local discovery request and print its workflow payload."""
    parser = build_parser()
    arguments = parser.parse_args(argv)

    user_agent = cast(str | None, arguments.user_agent)
    if not user_agent:
        parser.error("--user-agent or SEC_USER_AGENT is required")

    ciks = cast(list[str], arguments.cik)
    form_values = cast(list[str] | None, arguments.form)
    filed_from_value = cast(str, arguments.filed_from)
    filed_to_value = cast(str, arguments.filed_to)

    try:
        filed_from = date.fromisoformat(filed_from_value)
        filed_to = date.fromisoformat(filed_to_value)
    except ValueError as error:
        parser.error(f"invalid ISO filing date: {error}")

    forms = (
        frozenset(FilingForm(value) for value in form_values)
        if form_values
        else frozenset(FilingForm)
    )
    request = DiscoveryRequest(
        issuers=tuple(
            IssuerReference(provider="sec", provider_issuer_id=cik) for cik in ciks
        ),
        forms=forms,
        filed_from=filed_from,
        filed_to=filed_to,
    )
    result = build_sec_discovery_service(user_agent).execute(request)
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
