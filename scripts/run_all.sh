#!/usr/bin/env bash
# Reproduce every check: fixtures, offline replays, and the full test suite.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --quiet --disable-pip-version-check -r requirements.lock
python -m pip install --quiet --disable-pip-version-check -e .

echo "== 1/4 regenerating synthetic fixtures =="
python scripts/generate_fixtures.py

echo
echo "== 2/4 offline replay: short fork (winning) =="
python -m reorgindex.replay.cli replay fixtures/short_fork/recording.json \
  --db run/short_fork.db --report run/reports/short_fork.json

echo
echo "== 3/4 offline replay: deep fork (rejected) + interrupted switch + consistency =="
python -m reorgindex.replay.cli replay fixtures/deep_fork/recording.json \
  --db run/deep_fork.db --report run/reports/deep_fork.json
python -m reorgindex.replay.cli replay fixtures/interrupt/recording.json \
  --db run/interrupt.db --report run/reports/interrupt.json
python -m reorgindex.replay.cli verify fixtures/short_fork/recording.json \
  --db run/short_fork.db

echo
echo "== 4/4 full pytest suite =="
python -m pytest -v
