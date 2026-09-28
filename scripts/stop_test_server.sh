#!/usr/bin/env bash
# Stop the loopback test server started by start_test_server.sh.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$ROOT/build/testrun/dhcpd.pid"
if [[ ! -f "$PID_FILE" ]]; then
  echo "no pid file at $PID_FILE (server not running?)"
  exit 0
fi
PID="$(cat "$PID_FILE")"
if kill -0 "$PID" 2>/dev/null; then
  # Kill the whole process group created by setsid.
  PGID="$(ps -o pgid= -p "$PID" | tr -d ' ')"
  kill -TERM "-$PGID" 2>/dev/null || kill -TERM "$PID" 2>/dev/null || true
  for _ in $(seq 1 30); do
    kill -0 "$PID" 2>/dev/null || break
    sleep 0.1
  done
  kill -0 "$PID" 2>/dev/null && kill -KILL "$PID" 2>/dev/null || true
  echo "stopped pid=$PID"
else
  echo "pid $PID not alive"
fi
rm -f "$PID_FILE"
