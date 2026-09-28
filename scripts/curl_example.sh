#!/usr/bin/env bash
# End-to-end curl walkthrough against a locally running service.
# Usage: make run  (in one terminal), then  ./scripts/curl_example.sh
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8000}"
RUN="run-curl-$(date +%s)"
echo ">> health"
curl -s "$BASE/health" | python3 -m json.tool

echo ">> validate the valid sample (run=$RUN)"
curl -s -X POST "$BASE/validate" -H 'Content-Type: application/json' \
  -H "X-Run-Id: $RUN" \
  -d @examples/sample_strings.json | python3 -m json.tool | head -40

echo ">> import the valid int32 sample"
CID=$(curl -s -X POST "$BASE/columns" -H 'Content-Type: application/json' \
  -d @examples/sample_int32.json | python3 -c 'import sys,json;print(json.load(sys.stdin)["column_id"])')
echo "column_id=$CID"

echo ">> slice with non-zero offset=3 length=4"
curl -s -X POST "$BASE/columns/$CID/slice" -H 'Content-Type: application/json' \
  -d '{"offset":3,"length":4}' | python3 -m json.tool

echo ">> validate the deliberately invalid offsets fixture (must be ok:false)"
curl -s -X POST "$BASE/validate" -H 'Content-Type: application/json' \
  -d @examples/sample_bad_offsets.json | python3 -c '
import sys,json
r=json.load(sys.stdin)
print("ok:", r["ok"], "categories:", r["failure_categories"])'
