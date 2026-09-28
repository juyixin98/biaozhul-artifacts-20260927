#!/usr/bin/env bash
# Run the independent test suite with verbose output.
set -euo pipefail
cd "$(dirname "$0")/.."
exec .venv/bin/python -m pytest tests/ -v "$@"
