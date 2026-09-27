#!/usr/bin/env bash
# 一键启动：（可选）建虚拟环境、装依赖、生成夹具并播种、起服务。
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi
.venv/bin/pip -q install -r requirements.txt

# 仅在数据库不存在时播种，避免覆盖已有数据。
export POSTING_DB_PATH="${POSTING_DB_PATH:-$(pwd)/data/index.sqlite3}"
if [ ! -f "$POSTING_DB_PATH" ]; then
  .venv/bin/python scripts/generate_fixture.py
  .venv/bin/python -m scripts.seed_db
fi

echo "==> serving on http://127.0.0.1:8000  (docs: /docs)"
exec .venv/bin/uvicorn app.api.main:app --host 127.0.0.1 --port 8000
