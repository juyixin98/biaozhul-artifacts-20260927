#!/usr/bin/env bash
# 一键本地演示：建虚拟环境（若缺）-> 生成夹具 -> 跑测试 -> 起服务做一次实时请求。
set -euo pipefail
cd "$(dirname "$0")/.."

PY=.venv/bin/python
if [ ! -x "$PY" ]; then
  python3 -m venv .venv
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet -r requirements.txt
fi

export PYTHONPATH=src
echo "==> 1/4 生成合成夹具"
$PY scripts/build_fixtures.py

echo "==> 2/4 执行测试套件"
$PY -m pytest tests/

echo "==> 3/4 端到端演示（坏统计禁用剪枝后查询仍正确）"
$PY scripts/demo.py

echo "==> 4/4 启动 HTTP 服务 5 秒做健康检查（随后自动关闭）"
COLSTATS_SERVICE_DB_PATH=/tmp/colstats_demo.db \
  $PY -m uvicorn colstats.api:create_app --factory \
  --host 127.0.0.1 --port 8080 >/tmp/colstats_uvicorn.log 2>&1 &
SRV=$!
sleep 4
curl -s http://127.0.0.1:8080/health && echo
kill "$SRV" 2>/dev/null || true
echo "全部完成。"
