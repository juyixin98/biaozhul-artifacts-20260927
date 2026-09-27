#!/usr/bin/env bash
# deletable-cuckoo 端到端演示：构建（如需）-> 启动 -> 插入/查询/重复/删除/重放/统计。
# 完全使用本地合成数据；退出时清理子进程与临时目录。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BIN="$ROOT/target/release/cf-svc"
WORK="$(mktemp -d)"
PORT="${CF_DEMO_PORT:-$(( 20000 + RANDOM % 40000 ))}"
cleanup() {
  if [[ -n "${PID:-}" ]]; then kill "$PID" 2>/dev/null || true; wait "$PID" 2>/dev/null || true; fi
  rm -rf "$WORK"
}
trap cleanup EXIT

if [[ ! -x "$BIN" ]]; then
  echo "[demo] 未发现 release 二进制，先执行 cargo build --release ..."
  (cd "$ROOT" && cargo build --release --offline)
fi

sed -e "s|data_dir = \"./data\"|data_dir = \"$WORK/data\"|" \
    -e "s/port = 8080/port = $PORT/" \
    -e 's/run_id = ""/run_id = "demo-script"/' \
    "$ROOT/config/default.toml" > "$WORK/config.toml"

"$BIN" "$WORK/config.toml" >"$WORK/server.log" 2>&1 &
PID=$!
BASE="http://127.0.0.1:$PORT"

wait_http() {
  for _ in $(seq 1 100); do
    curl -sf "$BASE/healthz" >/dev/null && return 0
    sleep 0.1
  done
  echo "[demo] 服务未就绪，日志：" >&2; cat "$WORK/server.log" >&2; exit 1
}
wait_http
echo "[demo] 服务就绪：$BASE (run_id 见响应头 x-run-id)"

j() { python3 -m json.tool; }

echo -e "\n== 插入 alice（首次） =="
INS=$(curl -s -X POST "$BASE/filter/insert" -H 'Content-Type: application/json' -d '{"key":"alice"}')
echo "$INS" | j
TOKEN=$(echo "$INS" | python3 -c 'import sys,json;print(json.load(sys.stdin)["delete_token"])')

echo -e "\n== 重复插入 alice（计数 +1，不占新槽） =="
curl -s -X POST "$BASE/filter/insert" -H 'Content-Type: application/json' -d '{"key":"alice"}' | j

echo -e "\n== contains alice -> true =="
curl -s -X POST "$BASE/filter/contains" -H 'Content-Type: application/json' -d '{"key":"alice"}'; echo

echo -e "\n== 删除一份（凭令牌） =="
curl -s -X POST "$BASE/filter/delete" -H 'Content-Type: application/json' \
  -d "{\"delete_token\":\"$TOKEN\"}" | j

echo -e "\n== 重放同一令牌 -> 403 token_replayed =="
curl -s -w "\nHTTP %{http_code}\n" -X POST "$BASE/filter/delete" \
  -H 'Content-Type: application/json' -d "{\"delete_token\":\"$TOKEN\"}"

echo -e "\n== 统计 =="
curl -s "$BASE/stats" | j

echo -e "\n[demo] 完成。临时目录：$WORK（将自动清理）"
