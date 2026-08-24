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


## Run the application

```bash
uv run filing-corpus-pipeline
```


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
