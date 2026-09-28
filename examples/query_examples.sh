#!/usr/bin/env bash
# End-to-end call examples against a locally running service.
# Run first:  cargo run --release
# All examples use the synthetic fixture fixtures/sample_arrays.json ("mixed_small").
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:18080}"
RID_HEADER=(-H "X-Request-Id: example-manual-0001")

echo "== health =="
curl -s "${BASE}/health" | python3 -m json.tool

echo "== create index 'demo' from the mixed_small fixture =="
curl -s -X POST "${BASE}/v1/indexes" \
  "${RID_HEADER[@]}" \
  -H "Content-Type: application/json" \
  -d '{"name":"demo","values":[5,-3,7,7,0,-3,42,1]}' | python3 -m json.tool

echo "== list indexes =="
curl -s "${BASE}/v1/indexes" | python3 -m json.tool

echo "== k-th smallest (0-based): sorted window is [-3,-3,0,1,5,7,7,42] =="
curl -s -X POST "${BASE}/v1/indexes/demo/queries" \
  -H "Content-Type: application/json" \
  -d '{"op":"kth_smallest","l":0,"r":8,"k":3}' | python3 -m json.tool

echo "== count values < 8 over [0,8): expect 7 =="
curl -s -X POST "${BASE}/v1/indexes/demo/queries" \
  -H "Content-Type: application/json" \
  -d '{"op":"count_lt","l":0,"r":8,"bound":8}' | python3 -m json.tool

echo "== count values in [0, 8): expect 5 =="
curl -s -X POST "${BASE}/v1/indexes/demo/queries" \
  -H "Content-Type: application/json" \
  -d '{"op":"count_range","l":0,"r":8,"lo":0,"hi":8}' | python3 -m json.tool

echo "== predecessor of 7 (largest value < 7): expect 5 =="
curl -s -X POST "${BASE}/v1/indexes/demo/queries" \
  -H "Content-Type: application/json" \
  -d '{"op":"predecessor","l":0,"r":8,"bound":7}' | python3 -m json.tool

echo "== successor of 7 (smallest value >= 7): expect 7 =="
curl -s -X POST "${BASE}/v1/indexes/demo/queries" \
  -H "Content-Type: application/json" \
  -d '{"op":"successor","l":0,"r":8,"bound":7}' | python3 -m json.tool

echo "== failure example: k out of bounds -> 400 k_out_of_bounds =="
curl -s -w '\nHTTP %{http_code}\n' -X POST "${BASE}/v1/indexes/demo/queries" \
  -H "Content-Type: application/json" \
  -d '{"op":"kth_smallest","l":0,"r":8,"k":8}'

echo "== failure example: empty window [3,3) -> 400 empty_range =="
curl -s -w '\nHTTP %{http_code}\n' -X POST "${BASE}/v1/indexes/demo/queries" \
  -H "Content-Type: application/json" \
  -d '{"op":"count_lt","l":3,"r":3,"bound":0}'
