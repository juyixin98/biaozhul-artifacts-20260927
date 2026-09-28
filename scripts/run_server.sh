#!/usr/bin/env bash
# 启动 RTDA HTTP 服务。用法：./scripts/run_server.sh [端口]
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -d .venv ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

export RTDA_WAREHOUSE="${RTDA_WAREHOUSE:-$(pwd)/.warehouse}"
PORT="${1:-8000}"

echo "warehouse: $RTDA_WAREHOUSE"
exec uvicorn app.api.app:app_factory --factory --host 0.0.0.0 --port "$PORT"
