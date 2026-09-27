#!/usr/bin/env bash
# 端到端示例：启动服务 -> 插入 -> 查询 -> 凭凭证删除 -> 重放被拒。
# 仅依赖 bash + curl，全部本地合成数据。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

BIN="target/debug/cuckoo-api"
CFG="config/demo.toml"
DATA_DIR="data"
KEY_FILE="$DATA_DIR/master.key"
BASE="http://127.0.0.1:${PORT:-8088}"
RID="demo-$(date +%s)-$$"

echo "[1/7] 构建"
cargo build -q -p cuckoo-api

mkdir -p "$DATA_DIR"
if [[ ! -f "$KEY_FILE" ]]; then
  # 32 字节本地随机主密钥（演示用；生产请由密钥管理提供）。
  head -c 32 /dev/urandom | base64 > "$KEY_FILE"
fi

echo "[2/7] 启动服务（后台）"
CKF_LOG="info" "$BIN" "$CFG" > "$DATA_DIR/server.log" 2>&1 &
SRV_PID=$!
trap 'kill "$SRV_PID" 2>/dev/null || true' EXIT

# 等待健康检查就绪。
for _ in $(seq 1 50); do
  if curl -sf "$BASE/health" -H "X-Request-Id: $RID-health" >/dev/null 2>&1; then
    break
  fi
  sleep 0.1
done

req() {  # req METHOD PATH JSON
  curl -sS -X "$1" "$BASE$2" \
    -H "content-type: application/json" \
    -H "X-Request-Id: $RID" \
    -d "$3"
}

echo "[3/7] 健康检查"
req GET /health '{}'
echo

echo "[4/7] 插入键 user-42（返回删除凭证，请保存）"
INS=$(req POST /v1/filter/insert '{"key":"user-42"}')
echo "$INS"
CRED=$(printf '%s' "$INS" | sed -n 's/.*"credential":"\([^"]*\)".*/\1/p')

echo "[5/7] 查询 user-42（期望 member=true）与 stranger（近似，可能假阳性）"
req POST /v1/filter/lookup '{"key":"user-42"}'; echo
req POST /v1/filter/lookup '{"key":"stranger-7"}'; echo

echo "[6/7] 无凭证删除被拒（期望 403 INVALID_CREDENTIAL）"
req POST /v1/filter/delete '{"key":"user-42","credential":"forged"}'; echo

echo "[7/7] 持凭证删除成功，随后重放被拒（期望 409 CREDENTIAL_EXHAUSTED）"
req POST /v1/filter/delete "{\"key\":\"user-42\",\"credential\":\"$CRED\"}"; echo
req POST /v1/filter/delete "{\"key\":\"user-42\",\"credential\":\"$CRED\"}"; echo

echo "统计:"
req GET /v1/filter/stats '{}'; echo
echo "完成。服务日志见 $DATA_DIR/server.log（run 与 X-Request-Id=$RID 可关联）"
