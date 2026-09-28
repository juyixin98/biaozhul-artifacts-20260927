#!/usr/bin/env bash
# First-run helper: create the virtualenv, install deps, build the fixture
# and run the test suite. Safe to re-run.
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

PYTHONPATH=. python scripts/init_fixture.py
echo
python -m pytest -q
echo
echo "Start the API with:"
echo "  PYTHONPATH=. python -m uvicorn sqlguard.api.app:app --host 127.0.0.1 --port 8080"
