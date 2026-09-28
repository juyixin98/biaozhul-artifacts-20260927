#!/usr/bin/env bash
# curl 调用示例。先启动服务：
#   . .venv/bin/activate
#   uvicorn lake_txn.api:create_app --factory --port 8000
set -euo pipefail
BASE=${BASE:-http://127.0.0.1:8000}

echo "== 建表 =="
curl -sS -X POST "$BASE/v1/tables" -H 'Content-Type: application/json' \
  -H 'X-Request-ID: curl-demo-1' -d '{
    "table":"orders",
    "columns":[
      {"name":"order_id","type":"int64"},
      {"name":"region","type":"string"},
      {"name":"amount","type":"float64"}
    ],
    "partition_column":"region"}' | python -m json.tool

echo "== 暂存（inline 由服务写 Parquet）=="
curl -sS -X POST "$BASE/v1/staging/files" -H 'Content-Type: application/json' \
  -H 'X-Request-ID: curl-demo-2' -d '{
    "table":"orders","request_id":"curl-cn",
    "files":[{"logical_name":"cn-0","mode":"inline",
      "records":[{"order_id":1,"region":"cn","amount":10.0}]}]}' | python -m json.tool

echo "== 基于 s0 追加 =="
curl -sS -X POST "$BASE/v1/commits" -H 'Content-Type: application/json' \
  -H 'X-Request-ID: curl-demo-3' -d '{
    "table":"orders","request_id":"curl-cn","kind":"APPEND",
    "base_snapshot_id":0,"files":["cn-0"]}' | python -m json.tool

echo "== 同分区陈旧追加（409 冲突，非最后写获胜）=="
curl -sS -X POST "$BASE/v1/staging/files" -H 'Content-Type: application/json' -d '{
    "table":"orders","request_id":"curl-clash",
    "files":[{"logical_name":"clash-0","mode":"inline",
      "records":[{"order_id":2,"region":"cn","amount":20.0}]}]}' >/dev/null
curl -sS -o /tmp/conflict.json -w "HTTP %{http_code}\n" -X POST "$BASE/v1/commits" \
  -H 'Content-Type: application/json' -d '{
    "table":"orders","request_id":"curl-clash","kind":"APPEND",
    "base_snapshot_id":0,"files":["clash-0"]}'
python -m json.tool /tmp/conflict.json

echo "== 查询提交状态（响应丢失后用 request_id 判定）=="
curl -sS "$BASE/v1/commits/curl-cn?table=orders" | python -m json.tool

echo "== 快照与清理台账 =="
curl -sS "$BASE/v1/tables/orders/snapshots" | python -m json.tool
curl -sS "$BASE/v1/maintenance/cleanup-ledger" | python -m json.tool
