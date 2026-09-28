#!/usr/bin/env bash
# 端到端示例调用。用法：
#   1) 启动服务（另开终端）：
#        ANON_ALLOW_EPHEMERAL_KEY=1 .venv/bin/uvicorn app.main:app --port 8000
#   2) 运行本脚本：
#        bash examples/call_api.sh
set -euo pipefail
BASE="${BASE:-http://127.0.0.1:8000}"

echo "== health =="
curl -s "$BASE/health" | python3 -m json.tool

echo "== analyze (成功用例) =="
curl -s -X POST "$BASE/analyze" \
  -H 'Content-Type: application/json' \
  --data @examples/analyze_request.json | python3 -m json.tool

echo "== analyze (k 不可达用例) =="
curl -s -X POST "$BASE/analyze" \
  -H 'Content-Type: application/json' \
  --data @tests/fixtures/unique_signatures.json | python3 -m json.tool

echo "== audit verify =="
curl -s "$BASE/audit/verify" | python3 -m json.tool
