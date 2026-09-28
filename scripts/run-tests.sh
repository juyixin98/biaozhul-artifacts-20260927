#!/usr/bin/env bash
#
# Reproducible test entry point for the lz77-blocks backend.
#
# It:
#   1. regenerates the deterministic synthetic fixtures (no network/random data);
#   2. assigns a monotonic RUN NUMBER shared by every test binary so the four
#      suites' logs group into one replay directory tests/test-logs/<run-id>/;
#   3. runs `cargo test` (unit + four integration suites);
#   4. aggregates the per-suite JSON logs into summary.json with totals and
#      every PASS/FAIL case, its intermediate state and judgement reason.
#
# Usage:   ./scripts/run-tests.sh
# Outputs: tests/test-logs/run-<id>/{oracle_crosscheck,store_contract,
#          resource_exhaustion,service_api,summary}.json
#
# Replaying one failure: open the case entry, the `state.fixture` / parameters
# and both implementations' categories are recorded there.
set -euo pipefail

cd "$(dirname "$0")/.."

PY=${PYTHON:-python3}
command -v "$PY" >/dev/null || { echo "python3 required (independent reference oracle)" >&2; exit 2; }
command -v cargo >/dev/null || { echo "cargo required" >&2; exit 2; }

LOG_ROOT="tests/test-logs"
mkdir -p "$LOG_ROOT"

# Monotonic run number across the whole checkout.
RUN_NUMBER=$(find "$LOG_ROOT" -maxdepth 1 -mindepth 1 -type d -name 'run-*' 2>/dev/null | wc -l | tr -d ' ')
RUN_NUMBER=$((RUN_NUMBER + 1))
RUN_ID="run-$(date -u +%Y%m%dT%H%M%SZ)-$(printf '%03d' "$RUN_NUMBER")"
RUN_DIR="$LOG_ROOT/$RUN_ID"
mkdir -p "$RUN_DIR"
export LZ77_TEST_RUN_ID="$RUN_ID"
export LZ77_TEST_RUN_NUMBER="$RUN_NUMBER"

echo "==> [$RUN_ID] run number $RUN_NUMBER"
echo "==> regenerating deterministic fixtures"
"$PY" reference/ref_lz77.py gen-fixtures --out tests/fixtures >/dev/null

echo "==> cargo test"
set +e
CARGO_OUTPUT=$(cargo test -- --test-threads=1 2>&1)
TEST_RC=$?
set -e
echo "$CARGO_OUTPUT"
echo "$CARGO_OUTPUT" > "$RUN_DIR/cargo-output.txt"

echo "==> aggregating logs"
"$PY" scripts/aggregate_logs.py "$RUN_DIR"

echo "==> summary: $RUN_DIR/summary.json"
"$PY" - "$RUN_DIR/summary.json" <<'PY'
import json, sys
s = json.load(open(sys.argv[1]))
print(f"run {s['run_id']} (#{s['run_number']}): "
      f"total={s['counts']['total']} pass={s['counts']['pass']} "
      f"fail={s['counts']['fail']} skip={s['counts']['skip']}")
for suite, c in s["suites"].items():
    print(f"  {suite:22s} pass={c['pass']:2d} fail={c['fail']:2d} skip={c['skip']:2d}")
PY

exit "$TEST_RC"
