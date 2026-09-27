#!/usr/bin/env bash
# Live demo: start the server, analyze the example programs, replay a
# counterexample. Requires jq for pretty output (optional).
set -euo pipefail

HOST="${SYMEX_HOST:-127.0.0.1:8080}"
BIN="${SYMEXD:-target/debug/symexd}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"

echo "== starting symexd on $HOST =="
SYMEX_BIND="$HOST" "$BIN" --config "$HERE/config/default.toml" &
SERVER_PID=$!
trap 'kill $SERVER_PID 2>/dev/null || true' EXIT
sleep 1

echo
echo "== GET /v1/health =="
curl -s "http://$HOST/v1/health"
echo

for prog in wrap_u8 safe_mask div_guard loop_sum; do
    echo
    echo "== POST /v1/analyze  examples/programs/$prog.sym =="
    SRC=$(cat "$HERE/examples/programs/$prog.sym")
    curl -s -X POST "http://$HOST/v1/analyze" \
        -H 'content-type: application/json' \
        -d "$(printf '{"source": %s}' "$(printf '%s' "$SRC" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')")" \
        | python3 -m json.tool | head -60
done

echo
echo "== POST /v1/replay  counterexample x=255 against wrap_u8 =="
curl -s -X POST "http://$HOST/v1/replay" \
    -H 'content-type: application/json' \
    -d "{\"source\": $(cat "$HERE/examples/programs/wrap_u8.sym" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))'), \"input\": {\"x\": 255}}" \
    | python3 -m json.tool

echo
echo "== done =="
