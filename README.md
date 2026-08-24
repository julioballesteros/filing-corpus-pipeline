# Filing Corpus Pipeline

Service and infrastructure for filing ingestion and processing.

## Requirements

- Python 3.13 or newer
- [uv](https://docs.astral.sh/uv/)

## Set up development

```bash
uv sync
uv run pre-commit install
uv run pre-commit run --all-files
```

Commit the generated `uv.lock` file so local development and CI use the same
resolved dependencies.


## Run discovery locally

```bash
export SEC_USER_AGENT="filing-corpus-pipeline your-email@example.com"

uv run filing-corpus-pipeline discover \
  --cik 0000320193 \
  --filed-from 2025-01-01 \
  --form 10-K \
  --form 10-Q
```

The command queries SEC submission metadata and prints the JSON-compatible
payload that the future parent Step Functions workflow will receive. It does
not download filing documents or write pipeline state.

SEC automated access requires a declared user agent. Use a real monitored
contact address and do not commit it to the repository.

## Initial code boundaries

- `domain`: provider-neutral filing records passed between pipeline stages.
- `discovery`: the discovery request, result, provider port, and use case.
- `adapters/sec`: SEC-specific HTTP response parsing and record mapping.
- `entrypoints`: thin runtime composition such as the local CLI and future
  Lambda handler.

Later acquisition and processing stages should consume `FilingReference`
records without depending on SEC response formats.


## Quality checks

```bash
uv run black .
uv run ruff check .
uv run mypy
uv run pytest
```

The same commands are available through `make format`, `make lint`,
`make typecheck`, `make test`, and `make check`.


GitHub Actions runs the complete check suite for pushes to `main` and pull
requests.


## Updating from the template

From a clean Git working tree:

```bash
uvx copier update
```

Review and test the resulting diff before committing it.
