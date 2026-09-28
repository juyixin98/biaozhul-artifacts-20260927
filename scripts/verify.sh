#!/usr/bin/env bash
# verify.sh — end-to-end verification for the dhcp4lab DHCPv4 subset.
#
# It runs, in order:
#   1. offline, reproducible build (vendored fixed-version deps)
#   2. go vet
#   3. the full test suite with -race and -count=1 (no cached results),
#      emitting structured per-run logs into test-results/
#   4. a live smoke test: starts the real server on loopback and drives a
#      four-way exchange + RELEASE through the HTTP replay interface,
#      asserting concrete results (not just "it answered").
#
# Everything is loopback-only; no production NIC is ever touched.
#
# Exit status is non-zero if ANY step fails. Checks that could not be
# executed are reported as SKIPPED/FAILED explicitly, never as passed.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

OUT="$ROOT/test-results"
mkdir -p "$OUT"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RUNLOG="$OUT/run-$STAMP.log"
export TESTLOG_FILE="$OUT/testlog-$STAMP.jsonl"

export GOPROXY="${GOPROXY:-off}"
MODFLAG="-mod=vendor"

log()  { printf '[verify] %s\n' "$*" | tee -a "$RUNLOG"; }
fail() { printf '[verify][FAIL] %s\n' "$*" | tee -a "$RUNLOG"; exit 1; }

log "dhcp4lab verification run $STAMP"
log "go: $(go version)"
log "repo: $ROOT"
log "goproxy: $GOPROXY (deps are vendored; network not required)"

# 1. Build -----------------------------------------------------------------
log "step 1/4: reproducible offline build"
if ! go build $MODFLAG -o "$OUT/dhcp4d" ./cmd/dhcp4d >>"$RUNLOG" 2>&1; then
  fail "build failed (see $RUNLOG)"
fi
VER="$("$OUT/dhcp4d" -version)"
log "built: $VER"

# 2. Vet -------------------------------------------------------------------
log "step 2/4: go vet"
if ! go vet $MODFLAG ./... >>"$RUNLOG" 2>&1; then
  fail "go vet failed (see $RUNLOG)"
fi

# 3. Full test suite -------------------------------------------------------
log "step 3/4: full test suite (-race, -count=1)"
set +e
go test $MODFLAG -race -count=1 -v ./... \
  >"$OUT/go-test-$STAMP.txt" 2>&1
TEST_RC=$?
set -e
# Summarize package results.
grep -E '^(ok|FAIL|---)' "$OUT/go-test-$STAMP.txt" | tee -a "$RUNLOG" || true
if [ "$TEST_RC" -ne 0 ]; then
  fail "go test failed (see $OUT/go-test-$STAMP.txt)"
fi

# 4. Live loopback smoke test ---------------------------------------------
log "step 4/4: live loopback smoke test via HTTP replay API"
SMOKE_LOG="$OUT/smoke-$STAMP.log"
if ! scripts/smoke.sh "$OUT/dhcp4d" "$SMOKE_LOG"; then
  fail "live smoke test failed (see $SMOKE_LOG)"
fi
tail -n 20 "$SMOKE_LOG" | tee -a "$RUNLOG"

log "ALL CHECKS PASSED"
log "artifacts: $OUT"
