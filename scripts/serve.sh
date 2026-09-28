#!/usr/bin/env bash
# 本地启动：bash scripts/serve.sh
# 配置见 config/config.yaml；可用 MERGE3_CONFIG=/path/to.yaml 覆盖。
set -euo pipefail
cd "$(dirname "$0")/.."
exec .venv/bin/python -m uvicorn merge3.main:app \
  --host "${HOST:-127.0.0.1}" --port "${PORT:-8000}" \
  --app-dir src
