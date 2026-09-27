#!/usr/bin/env bash
# Reproducible verification entry point.
#
#   ./scripts/verify.sh            # unit/integration tests + HTTP smoke + restart persistence
#   SKIP_SMOKE=1 ./scripts/verify.sh
#
# Every phase prints a labelled PASS/FAIL; the script exits non-zero on the
# first failing phase. A run id is generated and threaded through server logs
# and the HTTP smoke script so failures correlate inputs with the run.

set -u -o pipefail
cd "$(dirname "$0")/.."

RUN_ID="verify-$(date -u +%Y%m%dT%H%M%SZ)-pid$$"
echo "==[${RUN_ID}] prereg2d verification =="

phase() { echo; echo "--[${RUN_ID}] $*"; }
die()   { echo "!![${RUN_ID}] FAIL: $*"; exit 1; }

phase "cargo fmt --check"
cargo fmt --all -- --check || die "formatting differs; run: cargo fmt --all"

phase "cargo clippy (all targets)"
cargo clippy --all-targets --all-features -- -D warnings || die "clippy reported warnings"

phase "cargo test (deterministic seed; RUST_BACKTRACE=1)"
RUST_BACKTRACE=1 cargo test -- --nocapture 2>&1 | tee "target/${RUN_ID}-tests.log" \
  | grep -E "test result|FAIL|panicked"
grep -q "test result: FAILED" "target/${RUN_ID}-tests.log" && die "unit/integration tests failed"
echo "   test log: target/${RUN_ID}-tests.log"

if [ "${SKIP_SMOKE:-0}" = "1" ]; then
  echo "--[${RUN_ID}] SKIP_SMOKE set; HTTP smoke skipped"
  echo "==[${RUN_ID}] VERIFICATION PASSED (no smoke)"
  exit 0
fi

phase "build server binary"
cargo build --bin prereg2d-server || die "server build failed"

SMOKE_DIR="$(mktemp -d)"
PORT="$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')"
BASE="http://127.0.0.1:${PORT}"
SERVER_LOG="target/${RUN_ID}-server.log"
echo "   data dir: ${SMOKE_DIR}   base: ${BASE}"

phase "start server (fresh data dir)"
PREREG2D_DATA_DIR="${SMOKE_DIR}/data" PREREG2D_BIND="127.0.0.1:${PORT}" \
  ./target/debug/prereg2d-server >"${SERVER_LOG}" 2>&1 &
SERVER_PID=$!
trap 'kill ${SERVER_PID} 2>/dev/null || true' EXIT

# Wait for readiness (max ~5s).
for _ in $(seq 1 50); do
  curl -fsS "${BASE}/health" >/dev/null 2>&1 && break
  kill -0 "${SERVER_PID}" 2>/dev/null || { cat "${SERVER_LOG}"; die "server exited early"; }
  sleep 0.1
done
curl -fsS "${BASE}/health" >/dev/null || { cat "${SERVER_LOG}"; die "server did not become ready"; }

phase "HTTP smoke against live server"
python3 scripts/smoke_http.py "${BASE}" "${RUN_ID}" || {
  echo "---- server log tail ----"; tail -30 "${SERVER_LOG}"; die "HTTP smoke failed";
}

phase "restart persistence: stop, reopen, verify history survived"
kill "${SERVER_PID}" 2>/dev/null || true
wait "${SERVER_PID}" 2>/dev/null || true
echo "   CURRENT=$(tr -d '\n' <"${SMOKE_DIR}/data/CURRENT")  log lines=$(wc -l <"${SMOKE_DIR}/data/events.jsonl")"

PREREG2D_DATA_DIR="${SMOKE_DIR}/data" PREREG2D_BIND="127.0.0.1:${PORT}" \
  ./target/debug/prereg2d-server >>"${SERVER_LOG}" 2>&1 &
SERVER_PID=$!
for _ in $(seq 1 50); do
  curl -fsS "${BASE}/health" >/dev/null 2>&1 && break
  sleep 0.1
done

# After restart: head is v3 with sum 5; old v2 still sums to 8.
V3=$(curl -fsS "${BASE}/query?version=3&x_lo=-9223372036854775808&x_hi=9223372036854775807&y_lo=-9223372036854775808&y_hi=9223372036854775807")
V2=$(curl -fsS "${BASE}/query?version=2&x_lo=-9223372036854775808&x_hi=9223372036854775807&y_lo=-9223372036854775808&y_hi=9223372036854775807")
echo "   v3 after restart: ${V3}"
echo "   v2 after restart: ${V2}"
echo "${V3}" | grep -q '"version":3'   || die "head version did not survive restart"
echo "${V3}" | grep -q '"sum":5'        || die "v3 sum did not survive restart"
echo "${V2}" | grep -q '"sum":8'        || die "old v2 answer changed after restart"

# Appending after restart continues the version chain at v4.
V4=$(curl -fsS -X POST "${BASE}/batches" -H 'content-type: application/json' \
  -d '{"updates":[{"x":0,"y":2,"delta":5}]}')
echo "   post-restart batch: ${V4}"
echo "${V4}" | grep -q '"version":4' || die "version chain did not continue at v4"

phase "clean shutdown"
kill "${SERVER_PID}" 2>/dev/null || true
wait "${SERVER_PID}" 2>/dev/null || true
trap - EXIT

echo
echo "==[${RUN_ID}] VERIFICATION PASSED"
echo "   artifacts: ${SERVER_LOG}, target/${RUN_ID}-tests.log"
