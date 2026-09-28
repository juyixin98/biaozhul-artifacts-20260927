#!/usr/bin/env bash
# Start the dhcpv4lab test server on loopback with a fake clock, fresh SQLite
# database and structured logs. All sockets bind to 127.0.0.1 — never a
# production interface. The process id is written to build/testrun/dhcpd.pid.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
mkdir -p build/testrun

RUN_ID="${DHCPV4LAB_RUN_ID:-run-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
LOG="build/testrun/server-${RUN_ID}.log"

echo "building server..."
go build -trimpath \
  -ldflags "-X dhcpv4lab/internal/version.Commit=$(git rev-parse --short HEAD 2>/dev/null || echo unknown)" \
  -o build/testrun/dhcpd ./cmd/dhcpd

export DHCPV4LAB_FAKE_CLOCK=1
export DHCPV4LAB_RUN_ID="$RUN_ID"
export DHCPV4LAB_JSON_LOG="${DHCPV4LAB_JSON_LOG:-0}"

echo "starting: run_id=$RUN_ID config=configs/test.json log=$LOG"
# setsid keeps the process in its own group so stop_test_server.sh can kill it.
setsid nohup ./build/testrun/dhcpd --config configs/test.json >"$LOG" 2>&1 &
PID=$!
echo "$PID" > build/testrun/dhcpd.pid
echo "$RUN_ID" > build/testrun/run.id

# Wait for health.
for i in $(seq 1 50); do
  if curl -fsS "http://127.0.0.1:18080/healthz" >/dev/null 2>&1; then
    echo "ready pid=$PID"
    echo "run_id=$RUN_ID"
    exit 0
  fi
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "server exited early; tail of log:" >&2
    tail -n 30 "$LOG" >&2 || true
    exit 1
  fi
  sleep 0.1
done
echo "server did not become healthy in time" >&2
tail -n 30 "$LOG" >&2 || true
exit 1
