PYTHON ?= .venv/bin/python

.PHONY: install-dev format lint typecheck test check build hooks

install-dev:
	python3.12 -m venv .venv
	$(PYTHON) -m pip install -e ".[dev]"

format:
	$(PYTHON) -m ruff check --fix src tests scripts
	$(PYTHON) -m ruff format src tests scripts

lint:
	$(PYTHON) -m ruff format --check src tests scripts
	$(PYTHON) -m ruff check src tests scripts

typecheck:
	$(PYTHON) -m mypy

test:
	$(PYTHON) -m pytest --cov=src --cov-report=term --cov-fail-under=70

check:
	./scripts/check.sh

build:
	$(PYTHON) -m build

hooks:
	$(PYTHON) -m pre_commit install --install-hooks
