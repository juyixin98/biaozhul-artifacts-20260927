#!/usr/bin/env bash
# 首次使用：bash run.sh setup 之后 bash run.sh serve
set -euo pipefail
cd "$(dirname "$0")"

PY=.venv/bin/python

case "${1:-help}" in
  setup)
    python3 -m venv .venv
    .venv/bin/pip install --quiet --upgrade pip
    .venv/bin/pip install -r requirements.txt
    echo "setup ok: $($PY --version)"
    ;;
  test)
    mkdir -p logs
    $PY -m pytest "$@" 2>&1 | tee logs/pytest_$(date +%Y%m%dT%H%M%S).log
    ;;
  demo)
    $PY scripts/demo.py
    ;;
  demo-http)
    $PY scripts/demo.py --http
    ;;
  serve)
    exec $PY -m uvicorn table_merge.api:app \
      --app-dir src --host 127.0.0.1 --port "${PORT:-8000}" \
      --log-config config/uvicorn-log.yaml
    ;;
  *)
    echo "usage: bash run.sh {setup|test|demo|demo-http|serve}"
    ;;
esac
