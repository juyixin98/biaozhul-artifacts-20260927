#!/usr/bin/env bash
# Minimal curl walkthrough. Start the server first:
#   .venv/bin/uvicorn app.api:app --port 8077
# Works on a non-empty database too: the root snapshot id is read dynamically.
set -euo pipefail
BASE=${BASE:-http://127.0.0.1:8077}
FX=data/fixtures/events
T=${T:-curl_events_$(date +%s)}

post() { curl -sS -X POST "$BASE$1" -H 'content-type: application/json' -d "$2"; }

echo "--- create table partitioned by region,day"
ROOT=$(post "/tables" "$(python3 -c "import json,sys;print(json.dumps({'table':sys.argv[1],'partition_spec':['region','day']}))" "$T")" \
  | python3 -c "import json,sys;print(json.load(sys.stdin)['root_snapshot_id'])")
echo "table=$T root_snapshot_id=$ROOT"

echo "--- append us/2024-01-01 at root"
post /commits "$(python3 -c "
import json,sys
print(json.dumps({'table':sys.argv[1],'operation':'APPEND','request_id':'req-curl-us','base_snapshot_id':int(sys.argv[2]),'files':[sys.argv[3]]}))
" "$T" "$ROOT" "$PWD/$FX/us_20240101.parquet")" | python3 -m json.tool

echo "--- stale append of a DIFFERENT partition (eu) from root: rebased/accepted"
post /commits "$(python3 -c "
import json,sys
print(json.dumps({'table':sys.argv[1],'operation':'APPEND','request_id':'req-curl-eu','base_snapshot_id':int(sys.argv[2]),'files':[sys.argv[3]]}))
" "$T" "$ROOT" "$PWD/$FX/eu_20240101.parquet")" | python3 -m json.tool

echo "--- stale append of the SAME partition from root: 409 hard conflict"
post /commits "$(python3 -c "
import json,sys
print(json.dumps({'table':sys.argv[1],'operation':'APPEND','request_id':'req-curl-dup','base_snapshot_id':int(sys.argv[2]),'files':[sys.argv[3]]}))
" "$T" "$ROOT" "$PWD/$FX/us_20240101_v2.parquet")" | python3 -m json.tool

echo "--- replay the first request (lost response): same snapshot, replayed=true"
post /commits "$(python3 -c "
import json,sys
print(json.dumps({'table':sys.argv[1],'operation':'APPEND','request_id':'req-curl-us','base_snapshot_id':int(sys.argv[2]),'files':[sys.argv[3]]}))
" "$T" "$ROOT" "$PWD/$FX/us_20240101.parquet")" | python3 -m json.tool

echo "--- inspect latest snapshot rowset and the commit log"
curl -sS "$BASE/tables/$T/snapshots/latest" | python3 -m json.tool
curl -sS "$BASE/commits?table=$T" | python3 -m json.tool
