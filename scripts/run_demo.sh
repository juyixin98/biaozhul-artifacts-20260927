#!/usr/bin/env bash
# Local end-to-end demo: build, offline-analyze, start the HTTP service,
# upload a policy, replay witness packets, and show explainable traces.
#
# Requires only the Go toolchain (SQLite is the pure-Go modernc.org/sqlite).
# No network access or external accounts are needed.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
DB="$(mktemp -d)/fwrule-demo.db"
ADDR="127.0.0.1:18080"
BASE="http://${ADDR}"

echo "== 1. Build =="
go build -o bin/fwrule-server ./cmd/server
go build -o bin/fwrule-analyze ./cmd/analyze

echo
echo "== 2. Offline first-match analysis of configs/demo-policy.json =="
./bin/fwrule-analyze configs/demo-policy.json > /tmp/fwrule-demo-report.json
echo "diagnostic summary:"
jq -r '
  .rules[]
  | select((.kinds|length)>0)
  | "  \(.index) \(.rule_id): \(.kinds|join(",")) removable=\(.removable)"' /tmp/fwrule-demo-report.json

echo
echo "== 3. Start service (db=$DB) =="
./bin/fwrule-server -addr "$ADDR" -db "$DB" &
SRV=$!
trap 'kill $SRV 2>/dev/null || true' EXIT
for _ in $(seq 1 50); do
  curl -sf "$BASE/healthz" >/dev/null && break
  sleep 0.1
done

echo
echo "== 4. Upload policy -> version 1 =="
jq '{name: .name, spec: .}' configs/demo-policy.json \
  | curl -sf -X POST "$BASE/v1/policies" \
      -H 'Content-Type: application/json' -H 'X-Request-ID: demo-upload' -d @- \
  | jq '{version, name, findings: (.report.diagnostics|length)}'

replay() {
  local rid="$1"; shift
  echo "-- $rid --"
  curl -sf -X POST "$BASE/v1/replay" \
    -H 'Content-Type: application/json' \
    -d "$(jq -n --arg rid "$rid" "$@")" \
    | jq '.decision | {action, decided_by, family, protocol, trace: (.trace|map(select(.matched))|map(.rule_id))}'
}

echo
echo "== 5. Replay witness packets (request ids correlate with logs) =="
replay "w-shadow" \
  '{request_id:$rid, protocol:"tcp", src_ip:"10.0.0.1", dst_ip:"192.168.1.1", src_port:2000, dst_port:443}'

replay "w-partial" \
  '{request_id:$rid, protocol:"tcp", src_ip:"10.0.0.5", dst_ip:"192.168.1.1", src_port:2000, dst_port:443}'

replay "w-cross" \
  '{request_id:$rid, protocol:"tcp", src_ip:"172.16.0.1", dst_ip:"192.168.2.1", src_port:2000, dst_port:1700}'

replay "w-default-deny" \
  '{request_id:$rid, protocol:"tcp", src_ip:"8.8.8.8", dst_ip:"192.168.1.1", src_port:1, dst_port:22}'

echo
echo "== 6. Fetch the persisted explanation of one request =="
curl -sf "$BASE/v1/logs/w-cross" \
  | jq '{request_id, version, policy, decided_by: .decision.decided_by, trace_steps: (.decision.trace|length)}'

echo
echo "Demo complete. Database: $DB (ephemeral)."
