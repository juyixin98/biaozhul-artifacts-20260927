#!/usr/bin/env bash
# 本地一键验证：创建虚拟环境（若不存在）、安装依赖、运行全部测试。
# 仅使用本地合成夹具，不访问任何外部网络业务系统。
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt

echo "== 依赖版本 =="
python - <<'PY'
import fastapi, uvicorn, pydantic, cryptography, httpx, pytest
import sys
print("python", sys.version.split()[0])
for m in (fastapi, uvicorn, pydantic, cryptography, httpx, pytest):
    print(m.__name__, getattr(m, "__version__", "?"))
PY

echo "== 运行测试 =="
python -m pytest -v "$@"
