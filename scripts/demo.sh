#!/usr/bin/env bash
# demo.sh — local service-call walkthrough of replicactl.
#
# It builds the binary, starts the HTTP service on a throwaway SQLite file,
# drives the normal path (zero bootstrap -> load step -> stale noop), an
# injected fault (explicit failure category), and a real process restart
# (durable evidence history), then leaves the captured server log next to
# this script under results/.
#
# Requires only the Go toolchain (no CGO; pure-Go SQLite).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
RESULTS="$ROOT/results"
mkdir -p "$RESULTS"
# Pick an ephemeral free port (18080 may already be taken on shared hosts).
PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"
ADDR="127.0.0.1:$PORT"
DB="$WORK/demo.db"
SERVER_LOG="$RESULTS/live-run-server.log"
CALL_LOG="$RESULTS/live-run-calls.log"
CFG="$WORK/config.json"

cat > "$CFG" <<JSON
{
  "target_load_per_instance": 10,
  "max_scale_up_factor": 2,
  "max_scale_up_floor": 1,
  "scale_down_stable_window_seconds": 60,
  "stale_skew_seconds": 30,
  "tolerance": 0.10,
  "min_fresh_fraction": 0.5,
  "min_replicas": 0,
  "max_replicas": 16,
  "bootstrap_replicas": 1,
  "http_addr": "$ADDR",
  "database_dsn": "file:$DB"
}
JSON

echo "== build ==" | tee "$CALL_LOG"
(cd "$ROOT" && CGO_ENABLED=0 GOPROXY=off go build -o "$WORK/replicactl" ./app/cmd/replicactl)

echo "== start service (fresh db) ==" | tee -a "$CALL_LOG"
"$WORK/replicactl" -config "$CFG" >"$SERVER_LOG" 2>&1 &
PID=$!
trap 'kill $PID 2>/dev/null || true' EXIT

wait_ready() {
  for _ in $(seq 1 50); do
    curl -fsS "http://$ADDR/healthz" >/dev/null 2>&1 && return 0
    sleep 0.1
  done
  echo "service did not become ready"; exit 1
}
wait_ready

call() { # method path rid json
  local method="$1" path="$2" rid="$3" json="$4"
  echo "--> $method $path (X-Request-ID: $rid) $json" | tee -a "$CALL_LOG"
  if [ "$method" = "GET" ]; then
    curl -sS "http://$ADDR$path" -H "X-Request-ID: $rid" | tee -a "$CALL_LOG"
  else
    curl -sS -X "$method" "http://$ADDR$path" \
      -H "Content-Type: application/json" -H "X-Request-ID: $rid" \
      -d "$json" | tee -a "$CALL_LOG"
  fi
  echo | tee -a "$CALL_LOG"
}

T0=2000000
echo | tee -a "$CALL_LOG"
echo "== 1) zero fleet, no demand: explicit no-action reason ==" | tee -a "$CALL_LOG"
call POST /v1/reconcile demo-zero-nodemand "{\"at\": $T0}"

echo "== 2) fresh demand signal: independent zero policy bootstraps to 1 ==" | tee -a "$CALL_LOG"
call POST /v1/demand demo-demand "{\"present\": true, \"reported_at\": $T0}"
call POST /v1/reconcile demo-bootstrap "{\"at\": $T0}"

echo "== 3) one instance reports a load of 30 (T=10): raw=3, rate limited 1->2 ==" | tee -a "$CALL_LOG"
call POST /v1/metrics demo-m1 "{\"instance_id\": \"instance-001\", \"load\": 30, \"reported_at\": $T0}"
call POST /v1/reconcile demo-up-1 "{\"at\": $T0}"

echo "== 4) two old reports (40s > 30s skew), two instances silent: ZERO fresh -> no scale-up ==" | tee -a "$CALL_LOG"
# A fleet of 4 where every known report is stale and nobody is fresh: stale
# data must never trigger a scale-up, no matter how large the reported load.
call POST /v1/admin/seed demo-seed4 '{"replicas": 4}'
call POST /v1/metrics demo-m2a "{\"instance_id\": \"instance-001\", \"load\": 50, \"reported_at\": $((T0-40))}"
call POST /v1/metrics demo-m2b "{\"instance_id\": \"instance-002\", \"load\": 50, \"reported_at\": $((T0-40))}"
call POST /v1/reconcile demo-stale "{\"at\": $T0}"

echo "== 5) injected adapter fault: failure class is returned, not a 500 with no reason ==" | tee -a "$CALL_LOG"
# Fresh hot reports make the tick decide scale-up; the injected apply fault
# must surface as ADAPTER_APPLY_FAILED with HTTP 409.
call POST /v1/metrics demo-m3a "{\"instance_id\": \"instance-001\", \"load\": 30, \"reported_at\": $T0}"
call POST /v1/metrics demo-m3b "{\"instance_id\": \"instance-002\", \"load\": 30, \"reported_at\": $T0}"
call POST /v1/admin/faults demo-fault-set '{"set_replicas": "demo: simulated local adapter outage"}'
call POST /v1/reconcile demo-fault-tick "{\"at\": $T0}"
call POST /v1/admin/faults demo-fault-clear '{"set_replicas": ""}'

echo "== 6) decision log is correlated by request id ==" | tee -a "$CALL_LOG"
echo "--> GET /v1/requests/demo-bootstrap" | tee -a "$CALL_LOG"
curl -sS "http://$ADDR/v1/requests/demo-bootstrap" -H "X-Request-ID: demo-lookup" | tee -a "$CALL_LOG"
echo | tee -a "$CALL_LOG"

echo "== 7) real process restart on the same SQLite file ==" | tee -a "$CALL_LOG"
kill -TERM $PID; wait $PID 2>/dev/null || true
trap - EXIT
"$WORK/replicactl" -config "$CFG" >>"$SERVER_LOG" 2>&1 &
PID=$!
trap 'kill $PID 2>/dev/null || true' EXIT
wait_ready
call GET /v1/fixture demo-after-restart ''

echo | tee -a "$CALL_LOG"
echo "server log: $SERVER_LOG"
echo "call log:   $CALL_LOG"
echo "demo db (ephemeral): $DB"
