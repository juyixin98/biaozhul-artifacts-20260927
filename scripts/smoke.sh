#!/usr/bin/env bash
# End-to-end smoke against a running server: import -> nonzero-offset slice ->
# concat -> values, using one run id so every step is correlated in the DB/log.
set -euo pipefail
BASE="${BASE:-http://127.0.0.1:8000}"
RUN_ID="smoke-$(date +%s)"
H=(-H 'content-type: application/json' -H "x-run-id: $RUN_ID")

echo "run_id=$RUN_ID"
H1=$(curl -s "${H[@]}" -X POST "$BASE/api/v1/arrays/import" \
  -d '{"format":"pylist","type":"utf8","values":["alpha",null,"","βγ","",null,"z"]}' \
  | .venv/bin/python -c 'import sys,json; print(json.load(sys.stdin)["result"]["handle"])')
echo "imported: $H1"

SLICE=$(curl -s "${H[@]}" -X POST "$BASE/api/v1/arrays/slice" \
  -d "{\"handle\":\"$H1\",\"offset\":1,\"length\":5}")
echo "slice: $SLICE"
H2=$(echo "$SLICE" | .venv/bin/python -c 'import sys,json; print(json.load(sys.stdin)["result"]["handle"])')

echo "values:"
curl -s "${H[@]}" -X POST "$BASE/api/v1/arrays/values" -d "{\"handle\":\"$H2\"}"
echo

echo "concat:"
curl -s "${H[@]}" -X POST "$BASE/api/v1/arrays/concat" \
  -d "{\"handles\":[\"$H1\",\"$H2\"]}" \
  | .venv/bin/python -m json.tool | head -40

echo "run audit:"
curl -s "$BASE/api/v1/runs/$RUN_ID" | .venv/bin/python -m json.tool | head -40
