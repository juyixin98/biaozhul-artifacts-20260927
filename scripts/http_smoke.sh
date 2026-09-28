#!/usr/bin/env bash
# 端到端冒烟：启动服务器 → 注册 → 负权/重复增量批 → 历史版本查询 → 未注册拒绝 → 关闭。
# 使用系统 curl 与 python3（仅标准库），无外部账号/数据。
#
# 用法: scripts/http_smoke.sh [bind_addr]
set -euo pipefail

BIND="${1:-127.0.0.1:18080}"
HOST="http://${BIND}"
WORKDIR="$(mktemp -d -t pr2d-smoke-XXXXXX)"
trap 'kill "${SERVER_PID:-}" 2>/dev/null || true; rm -rf "$WORKDIR"' EXIT

BIN="${PR2D_BIN:-target/debug/pr2d-server}"
if [[ ! -x "$BIN" ]]; then
  echo "building server (cargo build)..."
  cargo build --bin pr2d-server >/dev/null
fi

echo "data dir: $WORKDIR"
PR2D_DATA_DIR="$WORKDIR" PR2D_BIND="$BIND" PR2D_LOG_LEVEL=info "$BIN" &
SERVER_PID=$!

# 等待端口就绪（最多 ~10s）
for _ in $(seq 1 100); do
  if curl -sf "$HOST/health" >/dev/null 2>&1; then break; fi
  sleep 0.1
done

echo "== health =="
curl -sS -D "$WORKDIR/h1" "$HOST/health" | python3 -m json.tool
RUN_ID=$(grep -i '^x-run-id:' "$WORKDIR/h1" | tr -d '\r' | awk '{print $2}')
echo "run id: $RUN_ID"

echo "== register (重复坐标: xs 有重复 1, ys 有重复 10) =="
curl -sS -X POST "$HOST/v1/tables" \
  -H 'X-Request-Id: smoke-register' \
  -H 'content-type: application/json' \
  -d '{"xs":[1,2,2,3],"ys":[10,20,20]}' | python3 -m json.tool

echo "== batch v1: (1,10)+12, (3,20)+5, (2,20)-3 =="
curl -sS -X POST "$HOST/v1/tables/1/batches" \
  -H 'content-type: application/json' \
  -d '{"updates":[{"x":1,"y":10,"delta":10},{"x":1,"y":10,"delta":2},{"x":3,"y":20,"delta":5},{"x":2,"y":20,"delta":-3}]}' \
  | python3 -m json.tool

echo "== batch v2: (1,10)-7, (3,20)+8 =="
curl -sS -X POST "$HOST/v1/tables/1/batches" \
  -H 'content-type: application/json' \
  -d '{"updates":[{"x":1,"y":10,"delta":-7},{"x":3,"y":20,"delta":8}]}' \
  | python3 -m json.tool

echo "== query v2 全域（期望 sum=15）与时间旅行 v1（期望 sum=14）=="
curl -sS -X POST "$HOST/v1/tables/1/query" \
  -H 'content-type: application/json' \
  -d '{"version":2,"x_lo":1,"x_hi":3,"y_lo":10,"y_hi":20}' | python3 -m json.tool
curl -sS -X POST "$HOST/v1/tables/1/query" \
  -H 'content-type: application/json' \
  -d '{"version":1,"x_lo":1,"x_hi":3,"y_lo":10,"y_hi":20}' | python3 -m json.tool

echo "== 未注册坐标必须 422（不是就近插入）=="
curl -sS -o "$WORKDIR/rej.json" -w 'HTTP %{http_code}\n' -X POST "$HOST/v1/tables/1/batches" \
  -H 'content-type: application/json' \
  -d '{"updates":[{"x":99,"y":10,"delta":1}]}'
python3 -m json.tool "$WORKDIR/rej.json"

echo "== 倒矩形必须 400 INVERTED_RECT =="
curl -sS -o "$WORKDIR/inv.json" -w 'HTTP %{http_code}\n' -X POST "$HOST/v1/tables/1/query" \
  -H 'content-type: application/json' \
  -d '{"x_lo":3,"x_hi":1,"y_lo":10,"y_hi":20}'
python3 -m json.tool "$WORKDIR/inv.json"

# 用 python 断言关键字段，失败立即退出
python3 - "$WORKDIR" <<'PY'
import json, sys, pathlib
wd = pathlib.Path(sys.argv[1])
rej = json.loads((wd / "rej.json").read_text())
inv = json.loads((wd / "inv.json").read_text())
assert rej["ok"] is False and rej["error"]["code"] == "COORDINATE_NOT_REGISTERED", rej
assert inv["ok"] is False and inv["error"]["code"] == "INVERTED_RECT", inv
print("smoke assertions passed")
PY

echo "ALL SMOKE STEPS OK (run id: $RUN_ID)"
