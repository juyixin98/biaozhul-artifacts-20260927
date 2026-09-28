#!/usr/bin/env bash
# One-shot end-to-end demo against a FRESH data directory.
# Usage: .venv/bin/python scripts/serve_demo.sh   OR   bash scripts/serve_demo.sh
set -euo pipefail
cd "$(dirname "$0")/.."

PY=.venv/bin/python
DATA=${ZINDEX_DEMO_DIR:-data/demo}
PORT=${PORT:-8000}

rm -rf "$DATA"
export ZINDEX_DATA_DIR="$DATA"
export ZINDEX_CATALOG_PATH="$DATA/catalog.sqlite"
export ZINDEX_LOG_FILE="$DATA/demo.log"
mkdir -p "$DATA"

echo "== starting uvicorn on :$PORT (data dir $DATA) =="
.venv/bin/uvicorn zindex.api:app --host 127.0.0.1 --port "$PORT" --log-level warning &
SRV=$!
trap 'kill $SRV 2>/dev/null || true' EXIT

# wait for health
for i in $(seq 1 50); do
  if curl -sf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then break; fi
  sleep 0.2
done

B="http://127.0.0.1:$PORT"
jq_or_cat() { if command -v python3 >/dev/null; then python3 -m json.tool; else cat; fi; }

echo; echo "== create schema =="
curl -s -X POST "$B/schemas" -H 'Content-Type: application/json' \
  -d '{"name":"points2d","dims":[{"name":"x","bits":16,"signed":true},{"name":"y","bits":16,"signed":true}]}' \
  | jq_or_cat

echo; echo "== ingest 40000 synthetic uniform rows (capacity 8192 -> 5 chunks) =="
curl -s -X POST "$B/schemas/points2d/ingest_synthetic" -H 'Content-Type: application/json' \
  -d '{"n":40000,"shape":"uniform","seed":20260928,"capacity":8192}' | jq_or_cat

echo; echo "== rewrite into 4096-row globally sorted chunks =="
curl -s -X POST "$B/schemas/points2d/rewrite" -H 'Content-Type: application/json' \
  -d '{"capacity":4096}' | jq_or_cat

echo; echo "== thin box at high budget =="
curl -s -X POST "$B/schemas/points2d/query" -H 'Content-Type: application/json' \
  -H 'X-Request-ID: demo-thin-001' \
  -d '{"lo":[-2000,-2000],"hi":[2000,2000],"max_intervals":256}' | jq_or_cat

echo; echo "== same box at budget 1 (conservative, uncertainty flagged) =="
curl -s -X POST "$B/schemas/points2d/query" -H 'Content-Type: application/json' \
  -d '{"lo":[-2000,-2000],"hi":[2000,2000],"max_intervals":1}' | jq_or_cat

echo; echo "== audit trail for demo-thin-001 =="
curl -s "$B/requests/demo-thin-001" | jq_or_cat

echo; echo "Demo complete. Logs at $DATA/demo.log"
