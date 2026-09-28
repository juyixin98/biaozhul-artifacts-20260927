#!/usr/bin/env bash
# 一键：建虚拟环境（若不存在）、装锁定依赖、跑全部测试。
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
. .venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r requirements.lock.txt

echo "== pytest =="
python -m pytest "$@"
