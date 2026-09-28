#!/usr/bin/env bash
# Start the SQL review API locally (read-only fixture, encrypted audit DB).
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi

if [ ! -f fixtures/shop.db ]; then
  .venv/bin/python scripts/build_fixture.py
fi

export SQLGUARD_HOST="${SQLGUARD_HOST:-127.0.0.1}"
export SQLGUARD_PORT="${SQLGUARD_PORT:-8080}"

exec .venv/bin/uvicorn sqlguard.app:app \
  --host "$SQLGUARD_HOST" --port "$SQLGUARD_PORT"
