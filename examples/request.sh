#!/usr/bin/env bash
# Examples for the pvsim replay backend. All traffic is loopback; the
# backend never dials an external peer.
#
# Usage:
#   examples/request.sh POST  <scenario.json> [run_id] [base_url]
#   examples/request.sh GET   <run_id> [base_url]
#   examples/request.sh TRACES|DECISIONS|DELIVERIES|SCENARIO <run_id> [base_url]
#   examples/request.sh REPLAY <run_id> [base_url]
#   examples/request.sh LIST [base_url]
set -euo pipefail

BASE="${PVSIM_URL:-http://127.0.0.1:8080}"

wrap() { # wrap a scenario file in the request envelope, one JSON object
  python3 -c 'import json,sys
runid=sys.argv[2]
env={"scenario":json.load(open(sys.argv[1]))}
if runid: env["run_id"]=runid
print(json.dumps(env))' "$1" "${2:-}"
}

cmd="${1:-POST}"
case "$cmd" in
  POST)
    file="${2:?usage: POST <scenario.json> [run_id] [base_url]}"
    rid="${3:-}"
    base="${4:-$BASE}"
    wrap "$file" "$rid" | curl -s -X POST "$base/runs" \
      -H 'Content-Type: application/json' --data-binary @-
    ;;
  GET)
    rid="${2:?usage: GET <run_id> [base_url]}"
    base="${3:-$BASE}"
    curl -s "$base/runs/$rid"
    ;;
  TRACES|DECISIONS|DELIVERIES|SCENARIO)
    rid="${2:?usage: $cmd <run_id> [base_url]}"
    base="${3:-$BASE}"
    sub=$(echo "$cmd" | tr '[:upper:]' '[:lower:]')
    curl -s "$base/runs/$rid/$sub"
    ;;
  REPLAY)
    rid="${2:?usage: REPLAY <run_id> [base_url]}"
    base="${3:-$BASE}"
    curl -s -X POST "$base/runs/$rid/replay"
    ;;
  LIST)
    base="${2:-$BASE}"
    curl -s "$base/runs"
    ;;
  *)
    echo "unknown command: $cmd" >&2
    exit 2
    ;;
esac
