#!/usr/bin/env bash
# 端到端示例调用：启动服务 -> 创建运行 -> 基线核验 -> 泛化建议 -> 审计查询。
# 依赖：服务已在 127.0.0.1:8080 运行（python -m anon_risk --port 8080）。
set -euo pipefail

BASE=${BASE:-http://127.0.0.1:8080}
ADMIN_TOKEN=${ANON_RISK_ADMIN_TOKEN:-dev-admin-token}
HERE=$(cd "$(dirname "$0")" && pwd)

echo "== 健康检查 =="
curl -sS "$BASE/health" | python3 -m json.tool

echo
echo "== 创建运行（合成小表） =="
CREATE=$(curl -sS -X POST "$BASE/runs" \
  -H 'Content-Type: application/json' \
  --data-binary @"$HERE/../fixtures/tiny.json")
echo "$CREATE" | python3 -m json.tool
RUN_ID=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["run_id"])' <<<"$CREATE")
TOKEN=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])' <<<"$CREATE")
echo "run_id=$RUN_ID"

echo
echo "== 基线（不泛化）k=2,l=2 风险核验 =="
curl -sS -X POST "$BASE/runs/$RUN_ID/evaluate?k=2&l=2" \
  -H "X-Run-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"levels":{"zip":0,"age":0}}' | python3 -m json.tool

echo
echo "== 泛化建议（穷举最优，k=2,l=2） =="
curl -sS -X POST "$BASE/runs/$RUN_ID/suggest" \
  -H "X-Run-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"k":2,"l":2}' | python3 -m json.tool

echo
echo "== 不可达阈值（k=2,l=3，明确 UNREACHABLE 而非错误/成功） =="
curl -sS -X POST "$BASE/runs/$RUN_ID/suggest" \
  -H "X-Run-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"k":2,"l":3}' | python3 -m json.tool

echo
echo "== 审计事件（管理令牌） =="
curl -sS "$BASE/audit/events?limit=10" \
  -H "X-Admin-Token: $ADMIN_TOKEN" | python3 -m json.tool
