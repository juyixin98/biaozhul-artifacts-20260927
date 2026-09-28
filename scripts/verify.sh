#!/usr/bin/env bash
# Reproducible offline verification.
#
#   ./scripts/verify.sh
#
# 1. creates .venv (if missing) and installs FIXED versions from
#    requirements.txt (uses a local wheel cache; no project accounts)
# 2. runs the full pytest suite
# 3. runs the end-to-end acceptance scenario script
# 4. runs the independent fingerprint cross-check
#
# No network calls to credential providers are made at any point. pip needs
# package index access only on first run; set PIP_OFFLINE=1 to forbid even
# that (requires a warm cache).
set -euo pipefail

cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python3}"
if [ ! -d .venv ]; then
  "$PYTHON" -m venv .venv
fi
# shellcheck disable=SC1091
. .venv/bin/activate

PIP_FLAGS=(--disable-pip-version-check)
if [ "${PIP_OFFLINE:-0}" = "1" ]; then
  PIP_FLAGS+=(--no-index)
fi
pip install "${PIP_FLAGS[@]}" -r requirements.txt >/dev/null

echo "=== Unit / integration tests ==="
python -m pytest -q

echo
echo "=== Independent reference implementation cross-check ==="
python tools/make_baseline.py --pepper-id
python -c "
import hashlib
expected = hashlib.sha256(
    'dev-pepper-do-not-use-in-production-opp275'.encode()).hexdigest()[:12]
import subprocess, sys
got = subprocess.run([sys.executable, 'tools/make_baseline.py',
                      '--pepper-id'], capture_output=True, text=True).stdout.strip()
assert got == expected, (got, expected)
print('independent pepper id agrees:', got)
"

echo
echo "=== End-to-end acceptance scenarios ==="
python scripts/verify.py

echo
echo "ALL VERIFICATION STEPS COMPLETED SUCCESSFULLY"
