#!/usr/bin/env bash
# Example requests against a locally running server (./scripts/run_local.sh).
# Uses only curl + python3 (for JSON parsing). Requires `jq`-free parsing.
set -euo pipefail
BASE="${BASE:-http://127.0.0.1:8000}"

echo "== health =="
curl -s "$BASE/health"; echo

echo "== create a 3-of-5 collection =="
SECRET_HEX=$(python3 -c "print('the quick brown fox'.encode().hex())")
CREATE=$(curl -s -X POST "$BASE/collections" \
  -H 'Content-Type: application/json' \
  -H 'X-Request-ID: req-demo-create' \
  -d "{\"secret_hex\":\"$SECRET_HEX\",\"threshold\":3,\"total\":5}")
echo "$CREATE" | python3 -m json.tool | head -20
CID=$(printf '%s' "$CREATE" | python3 -c "import sys,json;print(json.load(sys.stdin)['collection_id'])")
echo "collection_id=$CID"

echo "== recover with shares #1,#3,#5 (a valid threshold subset) =="
PAYLOAD=$(printf '%s' "$CREATE" | python3 -c "
import sys,json
c=json.load(sys.stdin)
print(json.dumps({'shares':[c['shares'][0],c['shares'][2],c['shares'][4]]}))
")
curl -s -X POST "$BASE/collections/$CID/recover" \
  -H 'Content-Type: application/json' \
  -H 'X-Request-ID: req-demo-recover' \
  -d "$PAYLOAD" | python3 -m json.tool

echo "== recover with only 2 shares -> rejected_insufficient_threshold =="
PAYLOAD2=$(printf '%s' "$CREATE" | python3 -c "
import sys,json
c=json.load(sys.stdin)
print(json.dumps({'shares':c['shares'][:2]}))
")
curl -s -X POST "$BASE/collections/$CID/recover" \
  -H 'Content-Type: application/json' \
  -d "$PAYLOAD2" | python3 -m json.tool

echo "== audit for the recover request =="
curl -s "$BASE/audit?request_id=req-demo-recover" | python3 -m json.tool
