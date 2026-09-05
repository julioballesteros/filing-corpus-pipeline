"""Tests for the dependency-free local discovery entry point."""

import json
from datetime import date

import pytest

from filing_corpus_pipeline import cli
from filing_corpus_pipeline.discovery import DiscoveryRequest, DiscoveryResult


class CapturingService:
    """Capture the request produced by argparse without using the network."""

    def __init__(self) -> None:
        self.request: DiscoveryRequest | None = None

    def execute(self, request: DiscoveryRequest) -> DiscoveryResult:
        self.request = request
        return DiscoveryResult(filings=(), issuers_scanned=len(request.issuers))


def test_cli_executes_local_discovery_and_prints_workflow_payload(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The CLI exercises the same use case that the Lambda handler will call."""
    service = CapturingService()
    seen_user_agents: list[str] = []

    def build_service(user_agent: str) -> CapturingService:
        seen_user_agents.append(user_agent)
        return service

    monkeypatch.setattr(cli, "build_sec_discovery_service", build_service)
    monkeypatch.setenv("SEC_USER_AGENT", "pipeline contact@example.com")

    cli.main(
        [
            "discover",
            "--cik",
            "320193",
            "--cik",
            "789019",
            "--filed-from",
            "2025-01-01",
            "--filed-to",
            "2025-12-31",
            "--form",
            "10-Q",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert output == {
        "filings": [],
        "filings_found": 0,
        "issuers_scanned": 2,
    }
    assert seen_user_agents == ["pipeline contact@example.com"]
    assert service.request is not None
    assert service.request.filed_from == date(2025, 1, 1)
    assert {form.value for form in service.request.forms} == {"10-Q"}


def test_cli_requires_a_declared_user_agent(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Local execution cannot accidentally make undeclared SEC requests."""
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)

    with pytest.raises(SystemExit, match="2"):
        cli.main(["discover", "--cik", "320193", "--filed-from", "2025-01-01"])

    assert "SEC_USER_AGENT is required" in capsys.readouterr().err


def test_cli_rejects_an_invalid_date(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Date parsing errors are reported before composing the SEC client."""
    with pytest.raises(SystemExit, match="2"):
        cli.main(
            [
                "discover",
                "--cik",
                "320193",
                "--filed-from",
                "not-a-date",
                "--user-agent",
                "pipeline contact@example.com",
            ]
        )

    assert "invalid ISO filing date" in capsys.readouterr().err
