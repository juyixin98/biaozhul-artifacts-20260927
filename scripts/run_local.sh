#!/usr/bin/env bash
# Local development runner. Usage: ./scripts/run_local.sh
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -r requirements.txt

export SHAMIR_DB_PATH="${SHAMIR_DB_PATH:-data/shamir.db}"
export SHAMIR_AUDIT_STDERR="${SHAMIR_AUDIT_STDERR:-1}"
mkdir -p data

echo "Starting server on http://127.0.0.1:8000 (docs at /docs)"
exec uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
