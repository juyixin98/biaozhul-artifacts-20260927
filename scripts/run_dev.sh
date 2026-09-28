#!/usr/bin/env bash
# 本地启动开发服务器（合成数据，默认 ./data 目录）。
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install -r requirements-lock.txt
fi

export TSS_ENV="${TSS_ENV:-dev}"
export TSS_DATA_DIR="${TSS_DATA_DIR:-./data}"
exec .venv/bin/uvicorn threshold_service.app:app --host 127.0.0.1 --port "${PORT:-8000}"
