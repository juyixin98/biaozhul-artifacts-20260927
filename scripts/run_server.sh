#!/usr/bin/env bash
# Start the localffg HTTP API locally (synthetic/local only).
set -euo pipefail
cd "$(dirname "$0")/.."

PY=.venv/bin/python
if [ ! -x "$PY" ]; then
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt
fi

CONFIG_ARG=""
if [ -f config.yaml ]; then CONFIG_ARG="--config config.yaml"; fi
exec .venv/bin/python -m localffg.cli $CONFIG_ARG serve
