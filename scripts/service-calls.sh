#!/usr/bin/env bash
#
# Live service-call examples for the lz77-blocks backend.
#
# Starts (or uses) a running server, exercises every route with curl, and prints
# normal + abnormal responses including the four distinct failure classes.
# Requires only bash, curl and base64 (coreutils).
#
# Usage:  ./scripts/service-calls.sh [base_url]
#         ./scripts/service-calls.sh                 # builds & starts its own server
#         BASE=http://127.0.0.1:8080 ./scripts/service-calls.sh "$BASE"  # reuse one
set -uo pipefail

BASE="${1:-}"
STARTED=0
STORE_DIR="target/smoke-store"
PORT=18099

if [ -z "$BASE" ]; then
  BASE="http://127.0.0.1:$PORT"
  rm -rf "$STORE_DIR"
  cargo build --release 2>/dev/null
  ./target/release/lz77-blocks --store "$STORE_DIR" --addr "127.0.0.1:$PORT" \
      >target/smoke-server.log 2>&1 &
  SERVER_PID=$!
  STARTED=1
  trap 'kill $SERVER_PID 2>/dev/null || true' EXIT
  for _ in $(seq 1 50); do
    curl -sf "$BASE/health" >/dev/null 2>&1 && break
    sleep 0.1
  done
fi

b64() { printf '%s' "$1" | base64 | tr -d '\n'; }
hr()  { printf '\n\033[1m=== %s ===\033[0m\n' "$1"; }

hr "1. service description"
curl -s "$BASE/" | head -c 400; echo

hr "2. health (empty store)"
curl -s "$BASE/health"; echo

ROOT_PLAIN="alpha alpha alpha alpha alpha alpha alpha"
CHILD_PLAIN="alpha alpha alpha continuation continuation"

hr "3. POST /blocks independent (201)"
ROOT_JSON=$(curl -s -w '\n%{http_code}' -X POST "$BASE/blocks" \
  -H 'content-type: application/json' \
  -d "{\"mode\":\"independent\",\"data\":\"$(b64 "$ROOT_PLAIN")\"}")
echo "$ROOT_JSON"
ROOT_ID=$(printf '%s' "$ROOT_JSON" | head -1 | sed -n 's/.*"id":"\([^"]*\)".*/\1/p')

hr "4. POST /blocks dependent with prev_id pin (201)"
CHILD_JSON=$(curl -s -w '\n%{http_code}' -X POST "$BASE/blocks" \
  -H 'content-type: application/json' \
  -d "{\"mode\":\"dependent\",\"prev_id\":\"$ROOT_ID\",\"data\":\"$(b64 "$CHILD_PLAIN")\"}")
echo "$CHILD_JSON"
CHILD_ID=$(printf '%s' "$CHILD_JSON" | head -1 | sed -n 's/.*"id":"\([^"]*\)".*/\1/p')

hr "5. GET /blocks (ordered list)"
curl -s "$BASE/blocks"; echo

hr "6. GET /blocks/:id/raw (decompress child only)"
curl -s "$BASE/blocks/$CHILD_ID/raw"; echo

hr "7. GET /chain/raw (concatenated decompression)"
curl -s "$BASE/chain/raw"; echo

hr "8. POST /validate good frame (200)"
FRAME=$(curl -s "$BASE/blocks/$CHILD_ID/frame" | base64 | tr -d '\n')
curl -s -X POST "$BASE/validate" -H 'content-type: application/json' \
  -d "{\"frame\":\"$FRAME\",\"mode\":\"dependent\",\"dictionary\":\"$(b64 "$ROOT_PLAIN")\"}"
echo

hr "9. ABNORMAL: dependent block on empty chain would be 409 (fresh store) -> stale pin here"
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/blocks" -H 'content-type: application/json' \
  -d "{\"mode\":\"dependent\",\"prev_id\":\"$ROOT_ID\",\"data\":\"$(b64 "late writer")\"}"

hr "10. ABNORMAL: bad JSON (400 input_error)"
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/blocks" -H 'content-type: application/json' \
  -d '{not json'

hr "11. ABNORMAL: unknown block id (404 not_found)"
curl -s -w '\nHTTP %{http_code}\n' "$BASE/blocks/blk-00000099/raw"

hr "12. ABNORMAL: validate 1 GiB declaration bomb (413 resource_exhausted)"
BOMB=$(base64 < tests/fixtures/malformed/bomb_declared_size.frame | tr -d '\n')
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/validate" -H 'content-type: application/json' \
  -d "{\"frame\":\"$BOMB\",\"mode\":\"independent\"}"

hr "13. ABNORMAL: validate CRC-corrupt frame (400 input_error)"
BADCRC=$(base64 < tests/fixtures/malformed/bad_crc.frame | tr -d '\n')
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/validate" -H 'content-type: application/json' \
  -d "{\"frame\":\"$BADCRC\",\"mode\":\"independent\"}"

hr "14. ABNORMAL: dependent frame with empty dictionary (409 state_conflict)"
DEP=$(base64 < tests/fixtures/malformed/dependent_missing_predecessor.frame | tr -d '\n')
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/validate" -H 'content-type: application/json' \
  -d "{\"frame\":\"$DEP\",\"mode\":\"dependent\",\"dictionary\":\"$(b64 "")\"}"

[ "$STARTED" = 1 ] && echo "(server log: target/smoke-server.log)"
true
