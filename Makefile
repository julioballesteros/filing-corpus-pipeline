.PHONY: install hooks format format-check lint lint-fix typecheck test \
	infra-format infra-format-check infra-init infra-validate infra-test check

install:
	uv sync --all-groups

hooks:
	uv run pre-commit install

format:
	uv run ruff check --fix .
	uv run black .
	terraform fmt -recursive infra/terraform

format-check:
	uv run black --check .
	terraform fmt -check -recursive infra/terraform

lint:
	uv run ruff check .

lint-fix:
	uv run ruff check --fix .

typecheck:
	uv run mypy

test:
	uv run pytest

infra-format:
	terraform fmt -recursive infra/terraform

infra-format-check:
	terraform fmt -check -recursive infra/terraform

infra-init:
	terraform -chdir=infra/terraform init -backend=false

infra-validate: infra-init
	terraform -chdir=infra/terraform validate

infra-test: infra-init
	terraform -chdir=infra/terraform test

check: format-check lint typecheck test infra-validate infra-test
