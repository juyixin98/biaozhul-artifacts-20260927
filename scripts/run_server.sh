#!/usr/bin/env bash
# Start the planner API locally.
set -euo pipefail
cd "$(dirname "$0")/.."
export APP_DB_PATH="${APP_DB_PATH:-$(pwd)/var/jobs.db}"
export APP_LOG_DIR="${APP_LOG_DIR:-$(pwd)/var/logs}"
exec .venv/bin/uvicorn app.api.app:app --host 127.0.0.1 --port "${APP_PORT:-8000}"
