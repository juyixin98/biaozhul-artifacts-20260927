#!/usr/bin/env bash
# Minimal end-to-end HTTP usage example against a locally running server.
#
#   1. cargo run --release -- serve 127.0.0.1:8080
#   2. ./examples/http-smoke.sh
#
# Uses only curl; every request passes a stable x-run-id so the responses
# can be joined with the application/test logs.
set -euo pipefail

# Always resolve fixture paths relative to the repository root (parent of
# this script's examples/ directory), regardless of the caller's cwd.
cd "$(dirname "$0")/.."

BASE=${BASE:-http://127.0.0.1:8080}
RID=${RID:-doc-smoke-001}
RULESET=${RULESET:-fixtures/rulesets/shop-v1.json}

req() { # METHOD PATH [JSON_BODY]
  local method=$1 path=$2 body=${3:-}
  if [[ -n $body ]]; then
    curl -sS -X "$method" "$BASE$path" \
      -H 'content-type: application/json' -H "x-run-id: $RID" -d "$body"
  else
    curl -sS -X "$method" "$BASE$path" -H "x-run-id: $RID"
  fi
  echo
}

echo "1) create monitor"
CREATE=$(req POST /monitors "$(python3 -c "
import json
print(json.dumps({'monitor_id':'doc-smoke','ruleset':json.load(open('$RULESET'))}))")")
echo "$CREATE"

echo "2) submit trigger and boundary responses"
req POST /monitors/doc-smoke/steps '{"index":0,"event":{"type":"order_placed","facts":{"order_id":"o-1","priority":9,"healthy":true}}}'
req POST /monitors/doc-smoke/steps '{"index":1,"event":{"type":"order_ack","facts":{"order_id":"o-1","healthy":true}}}'

echo "3) attempt an out-of-order step (state conflict, HTTP 409)"
req POST /monitors/doc-smoke/steps '{"index":9,"event":{"type":"x","facts":{}}}' || true

echo "4) submit remaining steps, then close"
req POST /monitors/doc-smoke/steps '{"index":2,"event":{"type":"shipped","facts":{"order_id":"o-1","healthy":true}}}'
req POST /monitors/doc-smoke/steps '{"index":3,"event":{"type":"invoiced","facts":{"order_id":"o-1","invoice_no":"INV-1","healthy":true}}}'
req POST /monitors/doc-smoke/close

echo "5) fetch final state (verdict must be satisfied)"
req GET /monitors/doc-smoke

echo "6) offline oracle evaluation of the same fixture trace"
python3 -c "
import json
trace = json.load(open('fixtures/traces/a_boundary_satisfied.json'))
print(json.dumps({'ruleset': json.load(open('$RULESET')), 'trace': trace['steps']}))" \
| curl -sS -X POST "$BASE/evaluate" -H 'content-type: application/json' --data-binary @-
echo

echo "7) evidence at cut 2, then verify the returned bundle (data field)"
EVIDENCE=$(req GET "/monitors/doc-smoke/evidence?snapshot_after=2")
echo "$EVIDENCE"
echo "$EVIDENCE" | python3 -c "
import json,sys,urllib.request
bundle = json.load(sys.stdin)['data']
req = urllib.request.Request(
  '$BASE/verify',
  data=json.dumps(bundle).encode(),
  headers={'content-type':'application/json'})
print(urllib.request.urlopen(req).read().decode())"
