#!/usr/bin/env bash
# 端到端冒烟：启动服务 -> 分片 -> 门限恢复 -> 不足门限拒绝 -> 混集合拒绝 -> 查审计。
# 仅依赖 curl，所有输入为本地合成值。
set -euo pipefail
cd "$(dirname "$0")/.."
BASE="${BASE:-http://127.0.0.1:8000}"

b64() { printf '%s' "$1" | base64 | tr -d '\n'; }

echo "== health =="
curl -s "$BASE/health"; echo

echo "== create 2-of-3 set =="
ISSUE=$(curl -s -X POST "$BASE/sets" -H 'content-type: application/json' \
  -d "{\"secret_b64\":\"$(b64 'smoke-test-secret')\",\"threshold\":2,\"share_count\":3}")
echo "$ISSUE" | python3 -m json.tool
SET_ID=$(echo "$ISSUE" | python3 -c 'import sys,json;print(json.load(sys.stdin)["set_id"])')
SHARES=$(echo "$ISSUE" | python3 -c 'import sys,json;print(json.dumps(json.load(sys.stdin)["shares"]))')

echo "== recover with first 2 shares (expect ACCEPTED) =="
PAYLOAD=$(echo "$SHARES" | python3 -c 'import sys,json;s=json.load(sys.stdin);print(json.dumps({"shares":s[:2]}))')
curl -s -X POST "$BASE/recover" -H 'content-type: application/json' -d "$PAYLOAD" | python3 -m json.tool

echo "== recover with 1 share (expect 403 BELOW_THRESHOLD) =="
PAYLOAD=$(echo "$SHARES" | python3 -c 'import sys,json;s=json.load(sys.stdin);print(json.dumps({"shares":s[:1]}))')
curl -s -o /tmp/tss_below.json -w "HTTP %{http_code}\n" -X POST "$BASE/recover" \
  -H 'content-type: application/json' -d "$PAYLOAD"
python3 -m json.tool /tmp/tss_below.json

echo "== create second set and mix shares (expect 422 MIXED_SET) =="
ISSUE2=$(curl -s -X POST "$BASE/sets" -H 'content-type: application/json' \
  -d "{\"secret_b64\":\"$(b64 'other-secret-zzz')\",\"threshold\":2,\"share_count\":3}")
MIXED=$(python3 - "$ISSUE" "$ISSUE2" <<'PY'
import json,sys
a=json.loads(sys.argv[1])["shares"][:1]
b=json.loads(sys.argv[2])["shares"][:1]
print(json.dumps({"shares":a+b}))
PY
)
curl -s -o /tmp/tss_mixed.json -w "HTTP %{http_code}\n" -X POST "$BASE/recover" \
  -H 'content-type: application/json' -d "$MIXED"
python3 -m json.tool /tmp/tss_mixed.json

echo "== audit for first set (expect fingerprints only, no secret text) =="
curl -s "$BASE/audit?set_id=$SET_ID" | python3 -m json.tool
