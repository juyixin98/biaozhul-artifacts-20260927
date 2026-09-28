#!/usr/bin/env bash
# 端到端服务调用示例：起服务 -> 提交短分叉夹具 -> 查询链状态/余额/确认深度/
# 重组区间/诊断 -> 触发一次最终性越界（观察 409）-> 重建核对 -> 关服务。
#
# 依赖：curl、jq（仅为美化输出；没有 jq 可把 | jq 去掉）。
set -euo pipefail

cd "$(dirname "$0")/.."
DB="data/demo.sqlite3"
rm -f "$DB" "$DB-wal" "$DB-shm"
mkdir -p data

PORT=8011
BASE="http://127.0.0.1:${PORT}"
RID="demo-$(date +%s)"

echo ">> 启动服务 (port ${PORT})"
PYTHONPATH=src .venv/bin/python -m uvicorn reorgindex.api:create_app --factory \
  --host 127.0.0.1 --port "${PORT}" \
  --app-dir src > /tmp/reorgindex-demo.log 2>&1 &
SRV=$!
trap 'kill ${SRV} 2>/dev/null || true' EXIT

# 等待健康
for _ in $(seq 1 50); do
  curl -sf "${BASE}/health" >/dev/null && break
  sleep 0.2
done

post() {  # post <request-id> <jsonl-line>
  curl -sS -X POST "${BASE}/blocks" \
    -H "Content-Type: application/json" -H "X-Request-ID: $1" \
    -d "$2"
}

echo; echo ">> 逐块提交 short_fork_wins 夹具"
while IFS= read -r line; do
  [ -z "${line:-}" ] && continue
  echo "---"
  post "${RID}" "${line}" | jq -c '{status: .outcome.status, h: .outcome.height, tip: (.outcome.tip_hash[0:8]), disc: (.outcome.disconnected|length)}'
done < fixtures/short_fork_wins.jsonl

echo; echo ">> 当前链状态（链尖/权威链/余额）"
curl -sS "${BASE}/chain/state" | jq '{tip_hash: .tip_hash[0:8], tip_height, finalized_height, balances, contribution_count, pending_count}'

echo; echo ">> 重组事件（回滚区间）"
curl -sS "${BASE}/reorgs" | jq '.reorgs[0] | {old_tip: .old_tip[0:8], new_tip: .new_tip[0:8], rollback_from_height, rollback_to_height, disconnected: (.disconnected|length), connected: (.connected|length)}'

GEN=$(head -1 fixtures/deep_fork_rejected.jsonl | jq -r .header.block_hash)
echo; echo ">> 触发最终性越界：重放 deep_fork_rejected 前 9 块到链高 8，再投深分叉"
# 前 9 块构成到高度 8 的权威链（第 10 块是回滚 7>D=6 的深分叉）
i=0
while IFS= read -r line && [ "$i" -lt 9 ]; do
  [ -z "${line:-}" ] && continue
  post "deep-setup-${i}" "${line}" >/dev/null
  i=$((i+1))
done < fixtures/deep_fork_rejected.jsonl
DEEP=$(sed -n '10p' fixtures/deep_fork_rejected.jsonl)
curl -sS -o /tmp/r1.json -w "http_status=%{http_code}（期望 409）\n" -X POST "${BASE}/blocks" \
  -H "Content-Type: application/json" -H "X-Request-ID: deep-fork" \
  -d "${DEEP}"
jq -c '{request_id, category: .error.category, rollback: .error.context.rollback_count, depth: .error.context.finality_depth}' /tmp/r1.json
echo "   链尖高度仍为 8（拒绝后状态不变）:"
curl -sS "${BASE}/chain/state" | jq -c '{tip_height, finalized_height}'

echo; echo ">> 畸形请求体演示 decode_error 类别"
curl -sS -o /tmp/r2.json -w "http_status=%{http_code}\n" -X POST "${BASE}/blocks" \
  -H "Content-Type: application/json" -d '{"hello":"world"}'
jq -c '{category: .error.category}' /tmp/r2.json

echo; echo ">> 诊断事件（最近 5 条，携带 request_id 与关键状态，敏感字段已脱敏）"
curl -sS "${BASE}/diagnostics?limit=5" | jq '.events[] | {event_id, request_id, event, height: .payload.height, reason: .payload.reason}'

echo; echo ">> 全量重建核对（在线派生索引 vs 从零重建）"
curl -sS -X POST "${BASE}/debug/rebuild-check" | jq '{ok, mismatches, online_balances: .current.balances, rebuilt_balances: .rebuilt.balances}'

echo; echo ">> 完成。服务日志: /tmp/reorgindex-demo.log"
