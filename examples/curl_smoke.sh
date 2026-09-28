#!/usr/bin/env bash
# 本地冒烟脚本：启动服务，依次发出 SAT / UNSAT / UNKNOWN / 证据篡改 四类请求。
# 依赖：cargo、curl、python3（只用于美化 JSON）。
set -euo pipefail

cd "$(dirname "$0")/.."

BIN_LOG="$(mktemp)"
trap 'kill "${SERVER_PID:-}" 2>/dev/null || true; rm -f "$BIN_LOG"' EXIT

CNF_HTTP_BIND=127.0.0.1:18080 cargo run --quiet >"$BIN_LOG" 2>&1 &
SERVER_PID=$!

echo "waiting for server..."
for _ in $(seq 1 50); do
  if curl -fsS http://127.0.0.1:18080/healthz >/dev/null 2>&1; then break; fi
  sleep 0.2
done

pretty() { python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin), indent=2, ensure_ascii=False))'; }

echo; echo "== 1) SAT（结构化子句） =="
curl -sS -X POST http://127.0.0.1:18080/solve \
  -H 'content-type: application/json' \
  --data @examples/requests/sat.json | pretty

echo; echo "== 2) UNSAT（DIMACS 文本，带消解证明） =="
curl -sS -X POST http://127.0.0.1:18080/solve \
  -H 'content-type: application/json' \
  --data @examples/requests/unsat_dimacs.json | pretty

echo; echo "== 3) UNKNOWN（0 次决策预算，绝不能报 UNSAT） =="
curl -sS -X POST http://127.0.0.1:18080/solve \
  -H 'content-type: application/json' \
  --data @examples/requests/unknown_budget.json | pretty

echo; echo "== 4) 篡改的模型必须被 /verify 拒绝 =="
curl -sS -X POST http://127.0.0.1:18080/verify \
  -H 'content-type: application/json' \
  --data @examples/requests/verify_tampered_model.json | pretty

echo; echo "server log (metadata only, no formula payloads):"
cat "$BIN_LOG"
