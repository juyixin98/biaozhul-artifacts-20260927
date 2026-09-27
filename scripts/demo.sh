#!/usr/bin/env bash
# Reproducible local demo of the flexhash routing service.
# Uses only loopback addresses; no external services or accounts.
set -euo pipefail

PORT="${PORT:-19080}"
BASE="http://127.0.0.1:${PORT}"
DB="data/demo.db"
BIN="bin/flexhash"
CFG="configs/demo.json"

echo "== build =="
go build -o "$BIN" ./cmd/flexhash
mkdir -p data
rm -f "$DB" "$DB-wal" "$DB-shm"
# Point the demo config at the chosen port.
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
sed "s/127.0.0.1:[0-9]*/127.0.0.1:${PORT}/" "$CFG" > "$tmp"

"$BIN" -config "$tmp" >/tmp/flexhash-demo.log 2>&1 &
PID=$!
trap 'kill $PID 2>/dev/null || true; rm -f "$tmp"' EXIT

for _ in $(seq 1 50); do
  curl -sf -m 1 "$BASE/healthz" >/dev/null 2>&1 && break
  sleep 0.1
done

FLOW='{"src_ip":"10.1.2.3","src_port":51000,"dst_ip":"10.9.9.9","dst_port":443,"protocol":"tcp"}'

echo; echo "== 1. stable lookup (same flow twice) =="
curl -s -X POST "$BASE/v1/lookup" -H 'Content-Type: application/json' -d "$FLOW"; echo
curl -s -X POST "$BASE/v1/lookup" -H 'Content-Type: application/json' -d "$FLOW"; echo

echo; echo "== 2. structural shares =="
curl -s "$BASE/v1/shares"; echo

echo; echo "== 3. add hop-d (only affected buckets move) =="
curl -s -X POST "$BASE/v1/config" -H 'Content-Type: application/json' -d '{"members":[
  {"id":"hop-a","address":"127.0.0.1:9001","weight":3,"healthy":true},
  {"id":"hop-b","address":"127.0.0.1:9002","weight":2,"healthy":true},
  {"id":"hop-c","address":"127.0.0.1:9003","weight":1,"healthy":true},
  {"id":"hop-d","address":"127.0.0.1:9004","weight":1,"healthy":true}]}'; echo

echo; echo "== 4. mark hop-b down -> immediate weighted failover =="
curl -s -X POST "$BASE/v1/members/hop-b/health" -H 'Content-Type: application/json' -d '{"healthy":false}'; echo
curl -s -X POST "$BASE/v1/lookup" -H 'Content-Type: application/json' -d "$FLOW"; echo

echo; echo "== 5. repeated transition -> 409 state_conflict =="
curl -s -w ' [HTTP %{http_code}]\n' -X POST "$BASE/v1/members/hop-b/health" \
  -H 'Content-Type: application/json' -d '{"healthy":false}'

echo; echo "== 6. recover hop-b (versioned) -> flow returns home =="
curl -s -X POST "$BASE/v1/members/hop-b/health" -H 'Content-Type: application/json' -d '{"healthy":true}'; echo
curl -s -X POST "$BASE/v1/lookup" -H 'Content-Type: application/json' -d "$FLOW"; echo

echo; echo "== 7. replay verification =="
curl -s "$BASE/v1/replay/verify"; echo

echo; echo "== 8. input error example -> 400 =="
curl -s -w ' [HTTP %{http_code}]\n' -X POST "$BASE/v1/lookup" \
  -H 'Content-Type: application/json' -d '{"src_ip":"bad","dst_ip":"10.0.0.1","protocol":"tcp"}'

echo; echo "demo complete; server log at /tmp/flexhash-demo.log"
