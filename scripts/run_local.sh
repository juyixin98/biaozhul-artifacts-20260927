#!/usr/bin/env bash
# Start the R128 backend locally on http://127.0.0.1:8000
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export R128_DB_PATH="${R128_DB_PATH:-$ROOT/r128_jobs.db}"
export R128_MAX_JOB_BYTES="${R128_MAX_JOB_BYTES:-$((256 * 1024 * 1024))}"

exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --log-level info
