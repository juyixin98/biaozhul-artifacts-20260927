#!/usr/bin/env bash
# Service call examples for the local replica controller.
#
# Prerequisite: start the server with a short-window config so the scale-down
# behaviour is observable in seconds (the shipped default uses a 5m window):
#
#   go run ./cmd/replicactl -db file:demo.db -config configs/demo.fast.json -addr 127.0.0.1:18080 -no-autotick
#
# Every request carries an explicit X-Request-ID so logs and the decision
# audit record can be correlated end to end.
set -euo pipefail
BASE="${BASE:-http://127.0.0.1:18080}"

say() { printf '\n=== %s ===\n' "$1"; }
post() { # post <path> <json> [request-id]
  local path="$1" body="$2" rid="${3:-}"
  local args=(-sS -X POST "$BASE$path" -H 'Content-Type: application/json' -d "$body")
  if [[ -n "$rid" ]]; then args+=(-H "X-Request-ID: $rid"); fi
  curl "${args[@]}" | sed -e 's/^/  /'
  echo
}
get() { curl -sS "$BASE$1" ${2:+-H "X-Request-ID: $2"} | sed -e 's/^/  /'; echo; }

say "health"
get /healthz req-health

say "current fleet (expect 3 seeded replicas)"
get /v1/fleet req-fleet-initial

say "1) load step up: each of 3 instances reports 200 -> target 6"
post /v1/instances/ins-0001/samples '{"value":200}' req-sample-1
post /v1/instances/ins-0002/samples '{"value":200}' req-sample-2
post /v1/instances/ins-0003/samples '{"value":200}' req-sample-3
post /v1/reconcile '{}' req-step-up
get /v1/fleet req-fleet-up

say "2) delayed / stale report: high value already 90s old -> MUST hold"
post /v1/instances/ins-0004/samples '{"value":900,"observed_at":"'$(date -u -d '90 seconds ago' +%Y-%m-%dT%H:%M:%SZ)'"}' req-stale
post /v1/reconcile '{}' req-stale-tick

say "3) scale from zero is demonstrated separately; inspect the last decision"
get /v1/decisions/req-step-up

say "fetch recent explainable decisions (reasons/uncertainties per tick)"
get "/v1/decisions?limit=10" req-decisions
