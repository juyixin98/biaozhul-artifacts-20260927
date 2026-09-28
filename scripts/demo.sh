#!/usr/bin/env bash
# Local end-to-end demo for the difference-constraint service.
#
# It builds the server, starts it on 127.0.0.1:18080, and walks through:
#   1. health check
#   2. loading a feasible scheduling set (JSON + text DSL)
#   3. solving it and printing a concrete feasible assignment
#   4. tightening one constraint to create a negative cycle
#   5. solving again and printing the conflict (original constraint ids)
#   6. independently verifying the returned cycle via /v1/verify
#   7. showing each error class (400 / 409 / 413 / 422 / 404)
#
# Only bash + curl are required beyond cargo.
set -u

ADDR="${BIND_ADDR:-127.0.0.1:18080}"
BASE="http://${ADDR}/v1"
BIN="target/debug/diffconstraints-server"
LOG_FILE="/tmp/diffconstraints-demo.log"

c_green() { printf '\033[32m%s\033[0m\n' "$1"; }
c_blue()  { printf '\033[34m%s\033[0m\n' "$1"; }
c_red()   { printf '\033[31m%s\033[0m\n' "$1"; }

step() { echo; c_blue "── $1"; }

require() {
  command -v "$1" >/dev/null 2>&1 || { c_red "required command not found: $1"; exit 1; }
}
require cargo
require curl

c_blue "Building server (offline if deps cached)…"
cargo build --offline 2>/dev/null || cargo build

if [ -f "$LOG_FILE" ]; then : > "$LOG_FILE"; fi
BIND_ADDR="$ADDR" "$BIN" >"$LOG_FILE" 2>&1 &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null || true' EXIT

# Wait for readiness.
for _ in $(seq 1 100); do
  if curl -sf "$BASE/health" >/dev/null 2>&1; then break; fi
  sleep 0.1
done
if ! curl -sf "$BASE/health" >/dev/null 2>&1; then
  c_red "server did not start; see $LOG_FILE"
  exit 1
fi

step "1) health"
curl -s "$BASE/health"; echo

step "2) load feasible scheduling constraints (JSON)"
curl -s -X POST "$BASE/constraints" \
  -H 'Content-Type: application/json' \
  -d '{
    "constraints": [
      {"id":"b_start","lhs":"b","rhs":"a","bound":5},
      {"id":"c_start","lhs":"c","rhs":"b","bound":-2}
    ]
  }'; echo

step "2b) append via the text DSL (c - a <= 3 closes the chain)"
curl -s -X POST "$BASE/constraints" \
  -H 'Content-Type: application/json' \
  -d '{"text":"a_after: a - c <= 3   # a must start no later than c+3"}'; echo

step "3) solve -> feasible, concrete assignment (hand check: b-a<=5, c-b<=-2, a-c<=3)"
curl -s -X POST "$BASE/solve" -H 'Content-Type: application/json' -d '{}' \
  | python3 -m json.tool

step "4) tighten a_after to -4, creating the negative cycle 5-2-4 = -1"
curl -s -X POST "$BASE/constraints/update" \
  -H 'Content-Type: application/json' \
  -d '{"constraint":{"id":"a_after","lhs":"a","rhs":"c","bound":-4}}' >/dev/null
c_green "updated a_after bound: 3 -> -4"

step "5) solve -> infeasible; conflict uses ORIGINAL constraint ids only"
CONFLICT_JSON="$(curl -s -X POST "$BASE/solve" -H 'Content-Type: application/json' -d '{}')"
echo "$CONFLICT_JSON" | python3 -m json.tool
CYCLE_IDS="$(echo "$CONFLICT_JSON" | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["conflict"]["cycle_constraint_ids"]))')"

step "6) independent verification of the conflict cycle"
curl -s -X POST "$BASE/verify" \
  -H 'Content-Type: application/json' \
  -d "{\"cycle\":$CYCLE_IDS}" | python3 -m json.tool

step "7a) error taxonomy: 400 input (bad identifier)"
curl -s -o /tmp/r.json -w "HTTP %{http_code}\n" -X POST "$BASE/constraints" \
  -H 'Content-Type: application/json' \
  -d '{"constraints":[{"id":"1bad","lhs":"x","rhs":"y","bound":1}]}'
cat /tmp/r.json; echo

step "7b) 409 state_conflict (duplicate id)"
curl -s -o /tmp/r.json -w "HTTP %{http_code}\n" -X POST "$BASE/constraints" \
  -H 'Content-Type: application/json' \
  -d '{"constraints":[{"id":"b_start","lhs":"x","rhs":"y","bound":1}]}'
cat /tmp/r.json; echo

step "7c) 422 computation_failed (i64 overflow during relaxation)"
curl -s -X POST "$BASE/constraints/replace" -H 'Content-Type: application/json' \
  -d '{"constraints":[
    {"id":"drive","lhs":"b","rhs":"a","bound":-1},
    {"id":"min","lhs":"c","rhs":"b","bound":-9223372036854775808}
  ]}' >/dev/null
curl -s -o /tmp/r.json -w "HTTP %{http_code}\n" -X POST "$BASE/solve" \
  -H 'Content-Type: application/json' -d '{}'
cat /tmp/r.json; echo

step "7d) 413 resource_exhausted (body over 1 MiB)"
python3 - <<'PY'
import json, urllib.request
body = json.dumps({"text": "x" * 1_500_000}).encode()
req = urllib.request.Request("http://127.0.0.1:18080/v1/constraints", data=body,
                             headers={"Content-Type": "application/json"}, method="POST")
try:
    urllib.request.urlopen(req)
except urllib.error.HTTPError as e:
    print("HTTP", e.code)
    print(e.read().decode())
PY

step "7e) 404 not_found"
curl -s -o /tmp/r.json -w "HTTP %{http_code}\n" "$BASE/nope"
cat /tmp/r.json; echo

c_green "Demo complete. Server logs: $LOG_FILE; test run logs: test-results/runs.jsonl"
