"""Smoke tests for Filing Corpus Pipeline."""

import pytest

from filing_corpus_pipeline.__main__ import main


def test_main(capsys: pytest.CaptureFixture[str]) -> None:
    """The command-line entry point prints a greeting."""
    main()

    assert capsys.readouterr().out == "Hello from Filing Corpus Pipeline!\n"
