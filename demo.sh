#!/usr/bin/env bash
# Local demo for the weak trace inclusion checker (wtio).
#
# 1. Builds the project.
# 2. Runs the CLI over every fixture in fixtures/.
# 3. Starts the Axum server and calls /health and /api/v1/check.
# 4. Writes per-run JSONL diagnostics under ./demo-logs.
#
# No external accounts or network access required.
set -euo pipefail

cd "$(dirname "$0")"
ROOT="$(pwd)"
export WTIO_LOG_DIR="$ROOT/demo-logs"
rm -rf "$WTIO_LOG_DIR"
mkdir -p "$WTIO_LOG_DIR"

echo "==> cargo build"
cargo build

echo
echo "==> CLI checks"
for f in fixtures/*.json; do
  echo "---- $f"
  set +e
  cargo run -q -- check "$f" \
    | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception as e:
    print("  (non-JSON output)", e); sys.exit(0)
if "error" in d:
    print("  error:", d["error"]["kind"], d["error"]["code"], "-", d["error"]["message"])
else:
    print("  verdict:", d["verdict"])
    if d.get("counterexample"):
        ce = d["counterexample"]
        print("  shortest trace:", ce["trace"])
        print("  replay edges:", ce["implementation_replay"]["edge_count"])
        print("  verified:", d["verification"]["confirmed"])
    if d.get("unknown"):
        print("  unknown reason:", d["unknown"]["reason"])
    print("  run_id:", d["run_id"])
'
  set -e
done

echo
echo "==> start server"
PORT="${WTIO_PORT:-8080}"
cargo run -q -- serve --bind "127.0.0.1:$PORT" &
SERVER_PID=$!
trap 'kill $SERVER_PID 2>/dev/null || true' EXIT

# Wait for readiness.
for _ in $(seq 1 50); do
  if curl -sf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then break; fi
  sleep 0.2
done

echo "-- GET /health"
curl -s "http://127.0.0.1:$PORT/health" | python3 -m json.tool

echo "-- POST /api/v1/check  (vending machine — hidden free coffee)"
curl -s -X POST "http://127.0.0.1:$PORT/api/v1/check" \
  -H 'content-type: application/json' \
  --data @"$ROOT/fixtures/vending_machine.json" \
  | python3 -c '
import json, sys
d = json.load(sys.stdin)
print("verdict       :", d["verdict"])
print("alphabet      :", d["aligned_alphabet"])
ce = d["counterexample"]
print("shortest trace:", ce["trace"])
print("reason        :", ce["reason"])
print("confirmed     :", d["verification"]["confirmed"])
print("run_id        :", d["run_id"])
'

echo
echo "==> diagnostics written to $WTIO_LOG_DIR:"
ls -1 "$WTIO_LOG_DIR" | head
echo "Replay any run with:  python3 -m json.tool $WTIO_LOG_DIR/<run_id>.jsonl"
