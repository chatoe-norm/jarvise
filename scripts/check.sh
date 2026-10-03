#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ -n "${VIRTUAL_ENV:-}" && -x "$VIRTUAL_ENV/bin/python" ]]; then
  PYTHON="$VIRTUAL_ENV/bin/python"
elif [[ -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON="$ROOT/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
  PYTHON="$(command -v python)"
else
  echo "Python not found. Create .venv with Python 3.11+ and install -e '.[dev]'." >&2
  exit 127
fi

"$PYTHON" -m ruff format --check src tests scripts
"$PYTHON" -m ruff check src tests scripts
"$PYTHON" -m mypy

if (( $# > 0 )); then
  "$PYTHON" -m pytest --no-cov "$@"
else
  "$PYTHON" -m pytest --cov=src --cov-report=term --cov-fail-under=70
fi
