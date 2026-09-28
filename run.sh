#!/usr/bin/env bash
# Local development server. No external services required.
set -euo pipefail
cd "$(dirname "$0")"

export DICTSVC_SQLITE_PATH="${DICTSVC_SQLITE_PATH:-data/dictsvc.db}"
export DICTSVC_LOG_DIR="${DICTSVC_LOG_DIR:-data/logs}"

exec .venv/bin/python -m uvicorn dictsvc.api.app:app \
  --app-dir src --host 127.0.0.1 --port "${PORT:-8000}"
