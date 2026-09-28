#!/usr/bin/env bash
# Run the full test suite and write a replayable report to logs/.
set -euo pipefail
cd "$(dirname "$0")/.."

PY=.venv/bin/python
[ -x "$PY" ] || PY=python3

mkdir -p logs
STAMP="testrun-$(date -u +%Y%m%dT%H%M%SZ)"
echo "Running tests; report: logs/${STAMP}.log"
"$PY" -m pytest tests/ -v 2>&1 | tee "logs/${STAMP}.log"
echo
echo "Report saved: logs/${STAMP}.log"
