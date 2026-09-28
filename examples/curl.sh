#!/usr/bin/env bash
# Reproducible service-call walkthrough.
#
# Every call sends an explicit X-Request-Id; every response (success or
# failure) echoes it in the header and body so requests, answers and JSON
# log lines can be correlated.
#
# Usage:
#   ./target/release/wm-server --config config/wm.toml &   # in one terminal
#   bash examples/curl.sh                                  # in another
set -u
BASE="${BASE:-http://127.0.0.1:8080}"
# Counter persisted across the independent `curl` subshells so each request
# gets a distinct, correlated id.
COUNTER_FILE="$(mktemp -t wm-demo-rid.XXXXXX)"
trap 'rm -f "$COUNTER_FILE"' EXIT
rid() {
  local n
  n=$(($(cat "$COUNTER_FILE") + 1))
  echo "$n" > "$COUNTER_FILE"
  echo "demo-$(printf '%03d' "$n")"
}

echo "== 1. service info =="
curl -sS -H "X-Request-Id: $(rid)" "$BASE/" | head -c 600; echo

echo; echo "== 2. build index 'demo' (duplicates, negatives, signed extremes) =="
curl -sS -X POST "$BASE/indexes/demo" \
  -H "X-Request-Id: $(rid)" -H 'Content-Type: application/json' \
  -d '{"values":[5,2,5,0,-3,2,9223372036854775807,-9223372036854775808,-1,-1],"overwrite":false}' \
  | head -c 1200; echo

echo; echo "== 3. list indexes =="
curl -sS -H "X-Request-Id: $(rid)" "$BASE/indexes"; echo

echo; echo "== 4. k-th smallest of [0,10), k=3 (0-based); sorted [MIN,-3,-1,-1,0,2,2,5,5,MAX] -> -1 =="
curl -sS -X POST "$BASE/query" \
  -H "X-Request-Id: $(rid)" -H 'Content-Type: application/json' \
  -d '{"op":"quantile","index":"demo","l":0,"r":10,"k":3}'; echo

echo; echo "== 5. value range count [-1, 5) -> -1 twice, 0 once, 2 twice = 5 =="
curl -sS -X POST "$BASE/query" \
  -H "X-Request-Id: $(rid)" -H 'Content-Type: application/json' \
  -d '{"op":"count","index":"demo","l":0,"r":10,"lo":-1,"hi":5}'; echo

echo; echo "== 6. predecessor of 5 -> 2; successor of 5 -> 9223372036854775807 =="
curl -sS -X POST "$BASE/query" \
  -H "X-Request-Id: $(rid)" -H 'Content-Type: application/json' \
  -d '{"op":"predecessor","index":"demo","l":0,"r":10,"x":5}'; echo
curl -sS -X POST "$BASE/query" \
  -H "X-Request-Id: $(rid)" -H 'Content-Type: application/json' \
  -d '{"op":"successor","index":"demo","l":0,"r":10,"x":5}'; echo

echo; echo "== 7. ERROR: empty range [3,3) -> 422 EMPTY_RANGE =="
curl -sS -i -X POST "$BASE/query" \
  -H "X-Request-Id: $(rid)" -H 'Content-Type: application/json' \
  -d '{"op":"quantile","index":"demo","l":3,"r":3,"k":0}' | head -1
curl -sS -X POST "$BASE/query" \
  -H "X-Request-Id: $(rid)" -H 'Content-Type: application/json' \
  -d '{"op":"quantile","index":"demo","l":3,"r":3,"k":0}'; echo

echo; echo "== 8. ERROR: k out of bounds k=10, len 10 -> 422 K_OUT_OF_BOUNDS =="
curl -sS -X POST "$BASE/query" \
  -H "X-Request-Id: $(rid)" -H 'Content-Type: application/json' \
  -d '{"op":"quantile","index":"demo","l":0,"r":10,"k":10}'; echo

echo; echo "== 9. ERROR: index range beyond n -> 422 RANGE_OUT_OF_BOUNDS =="
curl -sS -X POST "$BASE/query" \
  -H "X-Request-Id: $(rid)" -H 'Content-Type: application/json' \
  -d '{"op":"quantile","index":"demo","l":0,"r":99,"k":0}'; echo

echo; echo "== 10. ERROR: unknown index -> 404 INDEX_NOT_FOUND =="
curl -sS -X POST "$BASE/query" \
  -H "X-Request-Id: $(rid)" -H 'Content-Type: application/json' \
  -d '{"op":"quantile","index":"ghost","l":0,"r":1,"k":0}'; echo

echo; echo "== 11. duplicate create without overwrite -> 409 INDEX_ALREADY_EXISTS =="
curl -sS -X POST "$BASE/indexes/demo" \
  -H "X-Request-Id: $(rid)" -H 'Content-Type: application/json' \
  -d '{"values":[1]}'; echo

echo; echo "== 12. build empty index -> 400 EMPTY_VALUES =="
curl -sS -X POST "$BASE/indexes/empty" \
  -H "X-Request-Id: $(rid)" -H 'Content-Type: application/json' \
  -d '{"values":[]}'; echo

echo; echo "== 13. malformed JSON -> 400 INVALID_JSON =="
curl -sS -X POST "$BASE/query" \
  -H "X-Request-Id: $(rid)" -H 'Content-Type: application/json' \
  -d '{not json'; echo

echo; echo "== 14. delete and confirm 404 =="
curl -sS -X DELETE -H "X-Request-Id: $(rid)" "$BASE/indexes/demo"; echo
curl -sS -H "X-Request-Id: $(rid)" "$BASE/indexes/demo"; echo
