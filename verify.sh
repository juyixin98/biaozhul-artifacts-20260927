#!/usr/bin/env bash
# 一键验证脚本：固定依赖 -> 生成合成夹具 -> 全量 pytest -> 离线回放（独立 oracle 对照）
# 退出码非 0 即存在未通过项。所有产物落到 logs/ 与 fixtures/。
set -euo pipefail

cd "$(dirname "$0")"

PY=.venv/bin/python
if [ ! -x "$PY" ]; then
  echo "[1/4] 创建虚拟环境 .venv"
  python3 -m venv .venv
fi

echo "[1/4] 安装/校验固定版本依赖"
.venv/bin/pip install -q -r requirements.txt

echo "[2/4] 生成本地合成夹具（预期结果由独立 oracle 计算）"
PYTHONPATH=src $PY scripts/gen_fixtures.py --out fixtures

mkdir -p logs
echo "[3/4] 运行全量测试套件（pytest）"
PYTHONPATH=src $PY -m pytest tests/ "$@"

echo "[4/4] 离线回放夹具（内核 vs 独立参考实现）"
PYTHONPATH=src $PY -m utxo_ledger.replay fixtures/validation_cases.json --log-dir logs

echo
echo "全部验证通过。运行日志: logs/<run_id>.jsonl；夹具: fixtures/"
