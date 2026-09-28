#!/usr/bin/env bash
# Start the arrowzero FastAPI service with the local venv and default config.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi

export PYTHONPATH="src${PYTHONPATH:+:$PYTHONPATH}"
export ARROWZERO_DB_PATH="${ARROWZERO_DB_PATH:-data/arrowzero.db}"
export ARROWZERO_LOG_PATH="${ARROWZERO_LOG_PATH:-logs/arrowzero.jsonl}"
HOST="${ARROWZERO_HOST:-127.0.0.1}"
PORT="${ARROWZERO_PORT:-8000}"

echo "arrowzero listening on http://$HOST:$PORT  (db=$ARROWZERO_DB_PATH log=$ARROWZERO_LOG_PATH)"
exec .venv/bin/uvicorn arrowzero.api.app:app --host "$HOST" --port "$PORT"
