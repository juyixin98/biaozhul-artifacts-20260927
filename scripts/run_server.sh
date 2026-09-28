#!/usr/bin/env bash
# 启动 HTTP 服务（默认 127.0.0.1:8000，可用环境变量覆盖）。
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
. .venv/bin/activate
export OT_DB_PATH="${OT_DB_PATH:-./ot.db}"
export OT_HOST="${OT_HOST:-127.0.0.1}"
export OT_PORT="${OT_PORT:-8000}"
exec python -m otbackend.api
