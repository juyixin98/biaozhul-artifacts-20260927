#!/usr/bin/env bash
# Service call examples for the cidrcov service.
# Assumes the server is running:  go run ./cmd/cidrcovd -config configs/service.json
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8080}"

echo "== health =="
curl -sS "$BASE/healthz" | tee /dev/stderr | head -c 200; echo

echo "== version =="
curl -sS "$BASE/version"; echo

echo "== basic IPv4 cover (from minimal fixture) =="
curl -sS -X POST "$BASE/v1/cover" \
  -H 'Content-Type: application/json' \
  --data @testdata/requests/basic_v4.json; echo

echo "== full IPv6 space minus the two extreme hosts =="
curl -sS -X POST "$BASE/v1/cover" \
  -H 'Content-Type: application/json' \
  --data @testdata/requests/full_v6.json; echo

echo "== mixed families =="
curl -sS -X POST "$BASE/v1/cover" \
  -H 'Content-Type: application/json' \
  --data @testdata/requests/mixed_families.json; echo

echo "== empty result (allow fully excluded) =="
curl -sS -X POST "$BASE/v1/cover" \
  -H 'Content-Type: application/json' \
  --data @testdata/requests/empty_result.json; echo

echo "== invalid entry: 422 with classified failure =="
curl -sS -o /tmp/cidrcov_invalid.json -w 'HTTP %{http_code}\n' \
  -X POST "$BASE/v1/cover" -H 'Content-Type: application/json' \
  --data @testdata/requests/invalid_entry.json
cat /tmp/cidrcov_invalid.json; echo

echo "== fetch the recorded request =="
curl -sS "$BASE/v1/requests/fixture-basic-v4" | head -c 400; echo

echo "== replay the recorded basic request deterministically =="
curl -sS -X POST "$BASE/v1/replay/fixture-basic-v4" \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); print("matches_recorded =", d["data"]["matches_recorded"])'

echo "== list recent requests =="
curl -sS "$BASE/v1/requests"; echo
