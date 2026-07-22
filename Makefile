.PHONY: install test lint typecheck check build standalone privacy e2e

E2E_BINARY ?= dist/continuity

install:
	uv sync --extra dev

test:
	uv run pytest --cov=continuity_kernel --cov-report=term-missing

lint:
	uv run ruff check .
	uv run ruff format --check .

typecheck:
	uv run mypy src tests scripts

privacy:
	uv run python scripts/privacy_check.py .

e2e:
	uv run python scripts/e2e_clean_install.py --binary "$(E2E_BINARY)"

check: lint typecheck test privacy

build:
	uv build

standalone:
	uv sync --extra dev --extra release
	uv run python scripts/build_standalone.py --output dist
