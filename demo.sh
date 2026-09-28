#!/usr/bin/env bash
# 本地演示脚本：构建并启动服务，逐步演示可行解、冲突证据、错误分类与证据验证。
# 用法：./demo.sh   （端口可用 PORT 覆盖，默认 8080；仅依赖 curl 与 python3）
set -euo pipefail

cd "$(dirname "$0")"

PORT="${PORT:-8080}"
BASE="http://127.0.0.1:${PORT}"
LOG_DIR="demo-logs"
SERVER_LOG="${LOG_DIR}/server.log"
mkdir -p "${LOG_DIR}"

# 优先用 jq，缺失则退回 python3（只负责美化输出）。
if command -v jq >/dev/null 2>&1; then
  pretty() { jq .; }
else
  pretty() { python3 -m json.tool; }
fi

step() { printf '\n========== %s ==========\n' "$*"; }
req() { # req <method> <path> [json-file]
  local method="$1" path="$2" body="${3:-}"
  if [ -n "${body}" ]; then
    curl -sS -o /tmp/dcs_body -w 'HTTP %{http_code}\n' \
      -X "${method}" -H 'content-type: application/json' \
      --data-binary "@${body}" "${BASE}${path}"
  else
    curl -sS -o /tmp/dcs_body -w 'HTTP %{http_code}\n' \
      -X "${method}" "${BASE}${path}"
  fi
  pretty < /tmp/dcs_body || cat /tmp/dcs_body
}

trap 'kill "${SERVER_PID:-0}" 2>/dev/null || true' EXIT

step "1/9 cargo build"
cargo build

step "2/9 启动服务 (127.0.0.1:${PORT}, 日志: ${SERVER_LOG})"
RUST_LOG="info,diff_constraints=debug" PORT="${PORT}" \
  ./target/debug/diff-constraints-service >"${SERVER_LOG}" 2>&1 &
SERVER_PID=$!
for _ in $(seq 1 50); do
  curl -sS "${BASE}/health" >/dev/null 2>&1 && break
  sleep 0.1
done
req GET /health

step "3/9 空集合"
req GET /v1/constraints

step "4/9 原子提交可行批次: a-b<=5, b-c<=-2, c-a<=1"
cat >/tmp/dcs_batch.json <<'JSON'
{"constraints":[
 {"name":"k1","x":"a","y":"b","c":5},
 {"name":"k2","x":"b","y":"c","c":-2},
 {"name":"k3","x":"c","y":"a","c":1}
]}
JSON
req POST /v1/constraints:batch /tmp/dcs_batch.json

step "5/9 取可行解（手算参考: a=0, b=-2, c=0）"
req GET /v1/solution

step "6/9 提交会导致不可满足的批次 n1:p-q<=-1, n2:q-p<=-1 -> 期望 409 + 严格负环"
cat >/tmp/dcs_bad.json <<'JSON'
{"constraints":[
 {"name":"n1","x":"p","y":"q","c":-1},
 {"name":"n2","x":"q","y":"p","c":-1}
]}
JSON
req POST /v1/constraints:batch /tmp/dcs_bad.json

step "7/9 集合保持原状（批次原子，无半更新）；独立验证一组一次性约束上的负环"
req GET /v1/constraints
cat >/tmp/dcs_verify.json <<'JSON'
{"cycle":["n1","n2"],"constraints":[
 {"name":"n1","x":"p","y":"q","c":-1},
 {"name":"n2","x":"q","y":"p","c":-1}
]}
JSON
req POST /v1/evidence/verify /tmp/dcs_verify.json

step "8/9 整数加法溢出 -> 期望 422 computation_failure"
cat >/tmp/dcs_overflow.json <<JSON
{"constraints":[
 {"name":"o1","x":"a","y":"b","c":-1},
 {"name":"o2","x":"x","y":"a","c":${INT_MIN:--9223372036854775808}}
]}
JSON
req POST /v1/solve /tmp/dcs_overflow.json

step "9/9 资源耗尽（129 个变量 > 128 上限）-> 期望 507"
python3 - <<'PY' >/tmp/dcs_quota.json
import json
rows = [{"name": f"v{i}", "x": f"v{i}", "y": f"v{i+1}", "c": 0} for i in range(128)]
json.dump({"constraints": rows}, open("/tmp/dcs_quota.json", "w"))
PY
req POST /v1/solve /tmp/dcs_quota.json

printf '\n演示完成；服务端结构化日志见 %s（含 run id、提交版本与判定理由）\n' "${SERVER_LOG}"
