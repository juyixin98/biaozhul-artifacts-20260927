#!/usr/bin/env bash
# Full local verification: regenerate independent golden vectors, then build
# and test the entire workspace offline.
#
# Exit status is non-zero if ANY test fails. A summary line at the end states
# explicitly what ran and what passed.
set -euo pipefail

cd "$(dirname "$0")/.."

echo "== [1/4] toolchain =="
rustc --version
cargo --version
python3 --version

echo "== [2/4] regenerate independent golden vectors (Python oracle) =="
python3 tests/reference/oracle.py emit --out tests/reference/fixtures.json

echo "== [3/4] cargo build (offline) =="
cargo build --offline --workspace

echo "== [4/4] cargo test (offline, all crates) =="
# Capture output while preserving exit code; print per-binary result lines.
set +e
OUT=$(cargo test --offline --workspace 2>&1)
RC=$?
set -e
echo "$OUT" | grep -E 'Running|test result|FAILED|panicked' || true

PASS=$(echo "$OUT" | grep -oE '[0-9]+ passed'  | awk '{s+=$1} END {print s+0}')
FAIL=$(echo "$OUT" | grep -oE '[0-9]+ failed'  | awk '{s+=$1} END {print s+0}')

echo
echo "================ SUMMARY ================"
echo "test binaries : $(echo "$OUT" | grep -c 'Running ')"
echo "passed total  : ${PASS}"
echo "failed total  : ${FAIL}"
if [ "$RC" -ne 0 ] || [ "$FAIL" -ne 0 ]; then
  echo "RESULT        : FAILED (cargo exit $RC)"
  exit 1
fi
echo "RESULT        : ALL GREEN"
