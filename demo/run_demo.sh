#!/usr/bin/env bash
# 本地端到端演示：
#   1. 构建并在后台启动 weak_trace_server
#   2. 依次发送 demo/requests/*.json
#   3. 打印判定，并把完整响应与服务端日志保存到 demo/out/，便于按 run_id 重放
#
# 用法：./demo/run_demo.sh [BIND_ADDR]
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIND="${1:-127.0.0.1:18080}"
HOST="${BIND%%:*}"
PORT="${BIND##*:}"
OUT_DIR="$ROOT/demo/out"
mkdir -p "$OUT_DIR"
SERVER_LOG="$OUT_DIR/server.log"

cd "$ROOT"

echo "==> 构建 debug 二进制"
cargo build --bin weak_trace_server

echo "==> 启动服务 http://$BIND"
BIND_ADDR="$BIND" RUST_LOG="info,weak_trace_inclusion=debug" \
  ./target/debug/weak_trace_server >"$SERVER_LOG" 2>&1 &
SERVER_PID=$!

cleanup() {
  kill "$SERVER_PID" >/dev/null 2>&1 || true
  wait "$SERVER_PID" 2>/dev/null || true
}
trap cleanup EXIT

# 等待 /health 就绪（最多约 10 秒）；服务进程中途退出则立即报错（常见于端口被占用）。
ready=0
for _ in $(seq 1 100); do
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "服务进程提前退出，日志末尾：" >&2
    tail -n 20 "$SERVER_LOG" >&2
    exit 1
  fi
  if curl -fsS "http://$HOST:$PORT/health" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 0.1
done
if [ "$ready" -ne 1 ]; then
  echo "等待服务健康检查超时，日志末尾：" >&2
  tail -n 20 "$SERVER_LOG" >&2
  exit 1
fi
curl -fsS "http://$HOST:$PORT/health" | sed 's/^/health: /'
echo

if command -v python3 >/dev/null 2>&1; then
  PRETTY=(python3 -m json.tool)
else
  PRETTY=(cat)
fi

for req in "$ROOT"/demo/requests/*.json; do
  base="$(basename "$req" .json)"
  resp_file="$OUT_DIR/${base}.response.json"
  echo "================================================================"
  echo "场景：$base"
  echo "请求：$req"
  http_code=$(curl -sS -o "$resp_file" -w "%{http_code}" \
    -H 'content-type: application/json' \
    --data-binary "@$req" "http://$HOST:$PORT/check")
  echo "HTTP $http_code"
  "${PRETTY[@]}" < "$resp_file" | {
    # 终端上只摘关键字段；完整 JSON 已存盘。
    grep -E '"(verdict|trace|length|shortest|reason_code|category|code|message|accepted|run_id|name|passed|reason)"' || true
  }
  echo "完整响应已保存：$resp_file"
  echo
done

echo "================================================================"
echo "服务端诊断日志（含运行编号、关键中间状态与判断理由）：$SERVER_LOG"
echo "可用 run_id 在日志中检索，例如：grep <run_id> $SERVER_LOG"
