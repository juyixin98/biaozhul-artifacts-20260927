#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [[ -d .venv ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

export PYTHONPATH="$(pwd)/src${PYTHONPATH:+:$PYTHONPATH}"

case "${1:-help}" in
  test)
    pytest -v "${@:2}"
    ;;
  selfcheck)
    python tools/selfcheck.py
    ;;
  replay)
    python -m abibackend.replay.cli --reset "${@:2}"
    ;;
  serve)
    exec python -m abibackend
    ;;
  *)
    cat <<'USAGE'
usage: bash run.sh <command>
  test       run the pytest suite (golden vectors vs eth_abi, security, kernel, API)
  selfcheck  run the dependency-free standard-library self-check
  replay     run the offline synthetic scenario into SQLite and print the report
  serve      start the FastAPI HTTP server (127.0.0.1:8080 by default)

environment:
  ABI_DB_PATH        SQLite file (default ./data/abibackend.sqlite3)
  ABI_API_HOST/PORT  bind address (default 127.0.0.1:8080)
  ABI_MAX_ALLOC_BYTES decode allocation ceiling (default 8388608)
USAGE
    ;;
esac
