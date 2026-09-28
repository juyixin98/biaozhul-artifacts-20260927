#!/usr/bin/env bash
# Build the local synthetic fixture and run the test suite.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi

.venv/bin/python scripts/build_fixture.py
.venv/bin/python -m pytest "$@"
