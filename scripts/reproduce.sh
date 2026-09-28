#!/usr/bin/env bash
# 一键复现：建虚拟环境 -> 安装锁定依赖 -> 生成夹具 -> 跑全部测试 ->
# 运行进程内演示 -> 启动真实 HTTP 服务跑 curl 正常/异常场景 -> 清扫演示。
# 所有输出保存到 results/，可复核。
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT=$(pwd)
RESULTS="$ROOT/results"
mkdir -p "$RESULTS"

echo ">>> [1/6] 准备虚拟环境与依赖"
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
. .venv/bin/activate
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.lock
python --version | tee "$RESULTS/00_environment.txt"
pip freeze | tee "$RESULTS/01_pip_freeze.txt" >/dev/null

echo ">>> [2/6] 生成最小合成夹具"
python -m fixtures.make_fixtures | tee "$RESULTS/02_fixtures.txt"

echo ">>> [3/6] 运行测试套件（含并发/覆盖/响应丢失/孤立文件/脱敏）"
rm -rf .local-data
python -m pytest -v 2>&1 | tee "$RESULTS/03_pytest.txt"

echo ">>> [4/6] 进程内服务调用演示"
python examples/usage_demo.py 2>/dev/null | tee "$RESULTS/04_usage_demo.txt"

echo ">>> [5/6] 启动真实 HTTP 服务并跑正常/异常场景"
rm -rf .local-data
# 自动选取一个空闲端口，避免与环境中已有服务冲突
PORT=$(python - <<'PY'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
)
LOG="$RESULTS/05_server_stderr.log"
echo "HTTP 服务端口: $PORT" | tee "$RESULTS/05_port.txt"
uvicorn lake_txn.api:create_app --factory --host 127.0.0.1 --port "$PORT" \
  >"$LOG" 2>&1 &
SERVER_PID=$!
trap 'kill $SERVER_PID 2>/dev/null || true' EXIT
for _ in $(seq 1 50); do
  curl -sf "http://127.0.0.1:$PORT/health" >/dev/null && break
  sleep 0.2
done
BASE="http://127.0.0.1:$PORT" bash examples/curl_example.sh \
  | tee "$RESULTS/06_http_curl.txt"
kill "$SERVER_PID" 2>/dev/null || true
wait "$SERVER_PID" 2>/dev/null || true
trap - EXIT

echo ">>> [6/6] 结果摘要"
{
  echo "测试结果:"
  tail -3 "$RESULTS/03_pytest.txt"
  echo
  echo "产物目录:"
  find .local-data -maxdepth 4 -type f 2>/dev/null | sort | sed 's#^#  #' || true
} | tee "$RESULTS/07_summary.txt"

echo
echo "完成。可复核输出见: $RESULTS/"
