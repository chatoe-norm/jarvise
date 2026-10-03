#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
ruff check src tests
mypy
python -m pytest --cov=src --cov-report=term --cov-fail-under=70 "$@"
