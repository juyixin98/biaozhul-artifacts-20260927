#!/usr/bin/env bash
# Reproducible verification for the offline secret candidate scanner.
#
# Runs, in order:
#   1. dependency check (versions pinned in requirements.txt)
#   2. deterministic fixture rebuild (all secrets are fake)
#   3. full pytest suite (62 tests), including:
#        - scanner core vs hand-authored expected answers
#        - state machine (new / active / known_fixed / reintroduced)
#        - content-fingerprint baseline surviving a file move
#        - deleted-candidate -> known_fixed classification
#        - text vs binary positions, oversize + unreadable handling
#        - log redaction and request identity correlation
#        - offline guarantee (socket construction blocked during scan)
#   4. CLI smoke: scan + report + audit on a throwaway snapshot
#
# This script performs NO network access itself apart from `pip` when run
# with --install; the scanner never opens sockets (asserted by the tests).
set -euo pipefail

cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
VENV_DIR="${VENV_DIR:-.venv}"

if [[ "${1:-}" == "--install" || ! -x "$VENV_DIR/bin/python" ]]; then
  echo "[1/4] creating virtualenv and installing pinned dependencies"
  "$PYTHON" -m venv "$VENV_DIR"
  "$VENV_DIR/bin/pip" install --quiet --upgrade pip
  "$VENV_DIR/bin/pip" install --quiet -r requirements.txt
else
  echo "[1/4] using existing $VENV_DIR (pass --install to force reinstall)"
fi
PY="$VENV_DIR/bin/python"

echo "[2/4] rebuilding deterministic synthetic fixture"
"$PY" tests/build_fixtures.py

echo "[3/4] running test suite"
"$PY" -m pytest

echo "[4/4] CLI smoke test"
SMOKE_ROOT="$(mktemp -d)/snap"
SMOKE_STATE="$(mktemp -d)/state"
mkdir -p "$SMOKE_ROOT"
printf 'AWS_ACCESS_KEY_ID = "AKIAFAKE000000000001"\n' > "$SMOKE_ROOT/conf.py"
SCAN_JSON="$("$PY" -m secretscan --state-dir "$SMOKE_STATE" scan "$SMOKE_ROOT" 2>/dev/null)"
echo "$SCAN_JSON" | "$PY" -c '
import json, sys
report = json.load(sys.stdin)["report"]
assert report["summary"]["candidates_total"] == 1, report["summary"]
c = report["candidates_new"][0]
assert c["masked"] == "AKIA…01"
assert c["locations"][0]["line"] == 1
assert "AKIAFAKE" not in json.dumps(report)
print("    CLI smoke OK; request_id =", report["scope"]["request_id"])
'

echo
echo "ALL CHECKS PASSED"
