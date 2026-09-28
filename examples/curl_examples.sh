#!/usr/bin/env bash
# 最小服务调用示例：正常 verify / submit，重复提交、异常分类。
# 前置：uvicorn service:app --host 127.0.0.1 --port 8332
set -u
BASE="${BASE:-http://127.0.0.1:8332}"
here="$(cd "$(dirname "$0")" && pwd)"
root="$(dirname "$here")"

j() { python3 -c "import json,sys; print(json.dumps(json.load(open(sys.argv[1]))['cases'][sys.argv[2]]['tx']))" \
      "$root/fixtures/cases.json" "$1"; }

echo "== health =="
curl -s "$BASE/health" | python3 -m json.tool

echo; echo "== verify case 00（合法，状态不应改变） =="
curl -s -X POST "$BASE/transactions/verify" -H 'Content-Type: application/json' \
  -d "$(j 00)" | python3 -m json.tool

echo; echo "== submit case 20（合法，201，UTXO 原子移动） =="
curl -s -i -X POST "$BASE/transactions/submit" -H 'Content-Type: application/json' \
  -d "$(j 20)" | head -1
curl -s -X POST "$BASE/transactions/submit" -H 'Content-Type: application/json' \
  -d "$(j 20)" | python3 -m json.tool

echo; echo "== 重复提交 case 20 → TX_ALREADY_ACCEPTED/STATE =="
curl -s -X POST "$BASE/transactions/submit" -H 'Content-Type: application/json' \
  -d "$(j 20)" | python3 -m json.tool

echo; echo "== case 05 重复签名 → SIG_DUPLICATED/COMPUTE =="
curl -s -X POST "$BASE/transactions/submit" -H 'Content-Type: application/json' \
  -d "$(j 05)" | python3 -m json.tool

echo; echo "== case 08 预算耗尽 → BUDGET_EXHAUSTED/RESOURCE =="
curl -s -X POST "$BASE/transactions/submit" -H 'Content-Type: application/json' \
  -d "$(j 08)" | python3 -m json.tool

echo; echo "== case 16 未知操作码 → UNKNOWN_OPCODE/INPUT =="
curl -s -X POST "$BASE/transactions/submit" -H 'Content-Type: application/json' \
  -d "$(j 16)" | python3 -m json.tool

echo; echo "== state（含离线回放比对） =="
curl -s "$BASE/state" | python3 -m json.tool
