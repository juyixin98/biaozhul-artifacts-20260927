#!/usr/bin/env bash
# 本地开发启动（首次先装依赖并生成黄金向量）。
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
. .venv/bin/activate
pip install -q -r requirements.lock

if [ ! -f tests/fixtures/golden_vectors.json ]; then
  python scripts/generate_golden_vectors.py
fi

export ABI_DB_PATH="${ABI_DB_PATH:-data/chain.db}"
export ABI_HOST="${ABI_HOST:-127.0.0.1}"
export ABI_PORT="${ABI_PORT:-8000}"
export ABI_RUN_ID="${ABI_RUN_ID:-dev-$(date +%Y%m%d%H%M%S)}"

echo "启动 http://$ABI_HOST:$ABI_PORT  (run_id=$ABI_RUN_ID, db=$ABI_DB_PATH)"
exec python -m uvicorn app.main:app --host "$ABI_HOST" --port "$ABI_PORT"
