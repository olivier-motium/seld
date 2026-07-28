.PHONY: install test lint typecheck check build standalone privacy e2e browser

E2E_BINARY ?= dist/gsv

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

browser:
	uv run python scripts/verify_bridge_browser.py
	uv run pytest tests/test_bridge_control_browser.py

check: lint typecheck test privacy

build:
	uv build

standalone:
	uv sync --extra dev --extra release
	uv run python scripts/build_standalone.py --output dist
