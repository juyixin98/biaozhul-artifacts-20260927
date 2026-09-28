#!/usr/bin/env bash
# 真实 HTTP API 示例调用。先启动服务：
#   uvicorn app.api:app --host 127.0.0.1 --port 8000
# 然后运行：bash scripts/example_calls.sh
set -euo pipefail
BASE=${BASE:-http://127.0.0.1:8000}
DOC=demo-$RANDOM

echo "== 建文档（初始文本 Hello） =="
curl -sS -X POST "$BASE/documents" \
  -H 'Content-Type: application/json' \
  -d "{\"doc_id\":\"$DOC\",\"text\":\"Hello\"}" | python3 -m json.tool

echo "== 客户端 c1 基于 rev0 在开头插入 [ （操作必须覆盖整个基线） =="
curl -sS -X POST "$BASE/documents/$DOC/ops" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: c1:1' \
  -d '{"client_id":"c1","client_seq":1,"base_revision":0,
       "components":[{"type":"insert","text":"[","client_id":"c1","seq":1},
                     {"type":"retain","n":5}]}' | python3 -m json.tool

echo "== c2 也基于 rev0 在开头并发插入（服务端 transform；同点按 client_id 排序） =="
curl -sS -X POST "$BASE/documents/$DOC/ops" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: c2:1' \
  -d '{"client_id":"c2","client_seq":1,"base_revision":0,
       "components":[{"type":"insert","text":"<","client_id":"c2","seq":1},
                     {"type":"retain","n":5}]}' | python3 -m json.tool

echo "== 拉取 rev0 之后的全部操作 =="
curl -sS "$BASE/documents/$DOC/ops?after=0" | python3 -m json.tool

echo "== 读取当前全文 =="
curl -sS "$BASE/documents/$DOC" | python3 -m json.tool

echo "== 裁剪到 rev2（写快照、删更早历史） =="
curl -sS -X POST "$BASE/documents/$DOC/prune?new_horizon=2" | python3 -m json.tool

echo "== 旧基线 rev0 再拉取 → 410 stale_baseline（必须重建基线） =="
curl -sS -i "$BASE/documents/$DOC/ops?after=0" | head -n 12

echo "== 诊断 =="
curl -sS "$BASE/diagnostics" | python3 -m json.tool
