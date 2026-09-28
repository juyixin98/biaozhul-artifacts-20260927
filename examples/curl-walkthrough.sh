#!/usr/bin/env bash
# End-to-end walkthrough against a running btmon server.
#
#   ./target/release/btmon serve          # terminal A (defaults 127.0.0.1:8080)
#   ./examples/curl-walkthrough.sh        # terminal B
#
# The script asserts concrete HTTP statuses and error codes for both the
# happy path and every error category, and tees all responses into
# results/http-walkthrough.log for later review. Requires curl + python3.
set -u

BASE="${BTMON_ADDR:-http://127.0.0.1:8080}"
RUN="run-$(date +%s)"
MID="walkthrough"
LOG="$(dirname "$0")/../results/http-walkthrough.log"
mkdir -p "$(dirname "$LOG")"
: > "$LOG"

pass=0; fail=0

# req METHOD PATH [JSON_BODY] [EXPECT_STATUS] [EXPECT_CODE]
req() {
  local method="$1" path="$2" body="${3:-}" want="${4:-200}" code="${5:-}"
  local args=(-sS -X "$method" -H "x-run-id: $RUN" -w $'\n%{http_code}')
  if [ -n "$body" ]; then
    args+=(-H "content-type: application/json" -d "$body")
  fi
  local raw status json
  raw="$(curl "${args[@]}" "$BASE$path")"
  status="${raw##*$'\n'}"
  json="${raw%$'\n'*}"
  echo "### $method $path -> $status (want $want)" | tee -a "$LOG"
  echo "$json" | tee -a "$LOG" >/dev/null
  if [ "$status" = "$want" ]; then pass=$((pass+1)); else
    fail=$((fail+1)); echo "  !! status mismatch" | tee -a "$LOG"; fi
  if [ -n "$code" ]; then
    local got
    got="$(printf '%s' "$json" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("error",{}).get("code",""))' 2>/dev/null)"
    if [ "$got" = "$code" ]; then pass=$((pass+1)); else
      fail=$((fail+1)); echo "  !! error code: got '$got' want '$code'" | tee -a "$LOG"; fi
  fi
}

echo "== run_id: $RUN ==" | tee -a "$LOG"

# 1. health
req GET /healthz

# 2. create with an explicit id and limits
RS="$(cat "$(dirname "$0")/ruleset-v1.json")"
req POST /monitors "{\"monitor_id\":\"$MID\",\"ruleset\":$RS,\"limits\":{\"max_active_obligations\":64,\"max_epochs\":8,\"max_log_bytes\":1048576}}"

# 3. duplicate create -> 409 state
req POST /monitors "{\"monitor_id\":\"$MID\",\"ruleset\":$RS}" 409 MONITOR_EXISTS

# 4. malformed JSON -> 400 input
req POST /monitors '{oops' 400 MALFORMED_JSON

# 5. invalid ruleset (within=0) -> 400 BAD_WINDOW
req POST /monitors '{"ruleset":{"version":"x","response":[{"id":"r","trigger":{"all":[]},"response":{"field":"k","op":"eq","value":1},"within":0}]}}' 400 BAD_WINDOW

# 6. not found
req GET /monitors/does-not-exist "" 404 NOT_FOUND

# 7. t0 order: spawns pay_after_order [0,2]; verdict wait
req POST "/monitors/$MID/events" '{"kind":"order"}'

# 8. t1 arm: sustain window [1,3], temp must stay <80; payment receipts
#    start being accepted (receipt window for the payment below)
req POST "/monitors/$MID/events" '{"kind":"arm","temp":70}'

# 9. t2 payment: satisfies the order obligation AND spawns receipt [3,4]
req POST "/monitors/$MID/events" '{"kind":"payment","temp":71}'

# 10. batch append: t3 receipt (satisfies receipt at first window step),
#     temp still safe
req POST "/monitors/$MID/events" '{"events":[{"kind":"receipt","temp":72}]}'

# 11. atomic batch: second event is invalid -> whole batch rejected,
#     next_step unchanged
req POST "/monitors/$MID/events" '{"events":[{"kind":"x","temp":1},{}]}' 400 EMPTY_EVENT

# 12. step regression -> 409 state. Per-event steps travel in the batch's
#     parallel `steps` array (an event's attributes are flattened, so a
#     `step` field inside an event is just data).
req POST "/monitors/$MID/events" '{"events":[{"kind":"late"}],"steps":[0]}' 409 STEP_REGRESSED

# 13. inspect obligations, rules, journal tail, and verify the chain
req GET "/monitors/$MID/obligations"
req GET "/monitors/$MID/rules"
req GET "/monitors/$MID/decisions?limit=5"
req POST "/monitors/$MID/verify"

# 14. snapshot, tamper check happens in the test suite; here fetch it
req GET "/monitors/$MID/snapshot"

# 15. rotate to v2 (different sustain rule); old pending obligations seal at
#     the boundary. The receipt obligation was already satisfied; nothing
#     epoch-0 is pending here, but rotation still records an epoch boundary.
req POST "/monitors/$MID/rotate" '{"version":"v2","response":[{"id":"ship_after_order","trigger":{"all":[{"field":"kind","op":"eq","value":"order"}]},"response":{"field":"kind","op":"in","value":["ship","air"]},"after":0,"within":2,"on_close":"strict"}]}'

# 16. rotating again to the SAME version -> 400 SAME_VERSION
req POST "/monitors/$MID/rotate" '{"version":"v2","response":[{"id":"r2","trigger":{"all":[]},"response":{"field":"k","op":"eq","value":1},"after":0,"within":2}]}' 400 SAME_VERSION

# 17. close: any surviving v2 pending -> strict violation
req POST "/monitors/$MID/end"

# 18. append after close -> 409 MONITOR_CLOSED
req POST "/monitors/$MID/events" '{"kind":"order"}' 409 MONITOR_CLOSED

# 19. resource exhaustion on a dedicated monitor (1 active obligation cap)
CAP_RS="$(cat "$(dirname "$0")/ruleset-cap.json")"
req POST /monitors "{\"monitor_id\":\"cap\",\"ruleset\":$CAP_RS,\"limits\":{\"max_active_obligations\":1,\"max_epochs\":2,\"max_log_bytes\":1048576}}"
req POST /monitors/cap/events '{"kind":"t"}'
req POST /monitors/cap/events '{"kind":"t"}' 507 OBLIGATION_LIMIT

echo
echo "== assertions: pass=$pass fail=$fail ==" | tee -a "$LOG"
[ "$fail" -eq 0 ]
