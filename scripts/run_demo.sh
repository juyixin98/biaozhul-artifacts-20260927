#!/usr/bin/env bash
# Reproduce the acceptance flow from a clean directory:
#   1. build, run the full test suite and clippy;
#   2. boot the real Axum server on an ephemeral local port;
#   3. send the three standard nets and one invalid request;
#   4. print each response (correlated by X-Run-Id) and the server log.
#
# Usage: scripts/run_demo.sh
# Requires: cargo/rustc (edition 2021), curl. No network or external accounts.
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PORT="${PN_DEMO_PORT:-18080}"
BIN="$ROOT/target/debug/pn-server"
LOG="$(mktemp -t pn-demo-XXXXXX.log)"

echo "== workspace =="
echo "$ROOT"
echo "== versions =="
rustc --version
cargo --version

echo
echo "== 1/3 build =="
cargo build -p pn-server

echo
echo "== 2/3 tests + clippy =="
cargo test --workspace
cargo clippy --workspace --all-targets -- -D warnings

echo
echo "== 3/3 start server on 127.0.0.1:${PORT} (log: $LOG) =="
PN_HTTP_BIND="127.0.0.1:${PORT}" PN_LOG_LEVEL=info "$BIN" >"$LOG" 2>&1 &
SRV=$!
trap 'kill "$SRV" >/dev/null 2>&1 || true' EXIT

# Wait for readiness.
for _ in $(seq 1 50); do
  if curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then break; fi
  sleep 0.1
done

call() {
  local name="$1" file="$2" runid="$3"
  echo
  echo "--- $name (run id: $runid) ---"
  curl -sS -X POST "http://127.0.0.1:${PORT}/api/v1/analyze" \
    -H 'content-type: application/json' \
    -H "x-run-id: $runid" \
    --data-binary "@$file" | sed 's/.{160}/&\n/g' | head -c 4000
  echo
}

call "mutex (reachable + in-box unreachable)" \
  examples/request-mutex.json "demo-mutex-0001"
call "producer/consumer (capacity-saturation deadlock)" \
  examples/request-producer-consumer.json "demo-pc-0002"
call "linear deadlock net" \
  examples/request-deadlock.json "demo-deadlock-0003"

echo
echo "--- invalid request (unknown place -> HTTP 422 SEMANTIC) ---"
curl -sS -o /tmp/pn-invalid.json -w "HTTP %{http_code}\n" \
  -X POST "http://127.0.0.1:${PORT}/api/v1/analyze" \
  -H 'content-type: application/json' \
  -H 'x-run-id: demo-invalid-0004' \
  --data-binary @examples/request-invalid-unknown-place.json
cat /tmp/pn-invalid.json

echo
echo "== correlated server log (run ids) =="
grep -E 'run_id="?demo-|listening|input rejected' "$LOG" || true
echo
echo "OK: demo finished. Full server log at $LOG"
