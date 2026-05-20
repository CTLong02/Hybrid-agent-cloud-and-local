#!/usr/bin/env bash
# Local CI: lint + format + types + tests + coverage gate.
# Run before committing.  Exits non-zero on any failure.

set -e
cd "$(dirname "$0")/.."

echo "==> ruff check"
ruff check hybrid_agent/

echo
echo "==> ruff format --check"
if ! ruff format --check hybrid_agent/; then
    echo "Run 'ruff format hybrid_agent/' to auto-fix." >&2
    exit 1
fi

echo
echo "==> mypy"
mypy hybrid_agent/

echo
echo "==> pytest (with coverage gate)"
python -m pytest tests/ --cov=hybrid_agent --cov-report=term -q

echo
echo "All checks passed."
