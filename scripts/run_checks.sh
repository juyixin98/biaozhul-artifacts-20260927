#!/usr/bin/env bash
# 本地验证入口：构建夹具 -> 跑测试 -> 打印日志位置。
# 用法：bash scripts/run_checks.sh
set -euo pipefail
cd "$(dirname "$0")/.."

PY=.venv/bin/python
if [ ! -x "$PY" ]; then
  echo "未发现 .venv，先执行：python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

echo "== 1/3 构建合成夹具 =="
$PY fixtures/build_fixtures.py

echo "== 2/3 运行测试套件 =="
$PY -m pytest

echo "== 3/3 测试运行日志（含 run_id / 版本 / 夹具 sha256 / 判定依据） =="
ls -t logs/testrun-*.jsonl | head -1
