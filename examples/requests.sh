#!/usr/bin/env bash
# Exercise the running service with representative local requests.
set -euo pipefail
BASE="${BASE:-http://127.0.0.1:8000}"

echo "== health =="
curl -s "$BASE/healthz" | python3 -m json.tool

echo "== versions =="
curl -s "$BASE/version" | python3 -m json.tool

echo "== encode: repeated dict items, cross-batch same local code =="
RESP=$(curl -s -X POST "$BASE/v1/encode" -H 'content-type: application/json' \
  -d '{
    "run_id": "demo-repeated",
    "target_width": 8,
    "width_policy": "reject",
    "batches": [
      {"batch_id": "b1",
       "dictionary": ["a", "b", "a"],
       "indices": [0, 1, 2, 0, null],
       "valid":   [true, true, true, true, false]},
      {"batch_id": "b2",
       "dictionary": ["z", "b"],
       "indices": [0, 1, 0],
       "valid":   [true, true, true]}
    ]
  }')
echo "$RESP" | python3 -m json.tool
RUN_ID=$(echo "$RESP" | python3 -c 'import sys,json;print(json.load(sys.stdin)["run_id"])')

echo "== empty + all-NULL batches =="
curl -s -X POST "$BASE/v1/encode" -H 'content-type: application/json' \
  -d '{"run_id":"demo-nulls","batches":[
      {"batch_id":"empty","dictionary":[],"indices":[],"valid":[]},
      {"batch_id":"all_null","dictionary":[],
       "indices":[null,null,null],"valid":[false,false,false]}]}' \
  | python3 -m json.tool

echo "== cardinality 257 vs 8-bit: reject (classified failure) =="
python3 - "$BASE" <<'PY'
import json, sys, urllib.request
base = sys.argv[1]
vals = list(range(257))
body = json.dumps({"run_id": "demo-overflow", "target_width": 8,
                   "batches": [{"batch_id": "over", "dictionary": vals,
                                "indices": vals, "valid": [True]*257}]}
                  ).encode()
req = urllib.request.Request(base + "/v1/encode", data=body,
                             headers={"content-type": "application/json"})
try:
    urllib.request.urlopen(req)
except urllib.error.HTTPError as e:
    print("HTTP", e.code, e.read().decode())
PY

echo "== cardinality 257 vs 8-bit: expand to 16 =="
python3 - "$BASE" <<'PY'
import json, sys, urllib.request
base = sys.argv[1]
vals = list(range(257))
body = json.dumps({"run_id": "demo-expand", "target_width": 8,
                   "width_policy": "expand",
                   "batches": [{"batch_id": "over", "dictionary": vals,
                                "indices": vals, "valid": [True]*257}]}
                  ).encode()
req = urllib.request.Request(base + "/v1/encode", data=body,
                             headers={"content-type": "application/json"})
out = json.load(urllib.request.urlopen(req))
print("global_index_width =", out["policy"]["global_index_width"],
      "cardinality =", out["global_dictionary"]["cardinality"])
PY

echo "== verify demo-repeated against independently supplied rows =="
curl -s -X POST "$BASE/v1/verify" -H 'content-type: application/json' \
  -d '{"run_id":"demo-repeated","batches":[
      {"batch_id":"b1","rows":["a","b","a","a",null]},
      {"batch_id":"b2","rows":["z","b","z"]}]}' \
  | python3 -m json.tool

echo "== fetch stored run metadata =="
curl -s "$BASE/v1/runs/$RUN_ID" | python3 -m json.tool
