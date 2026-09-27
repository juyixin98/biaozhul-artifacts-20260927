#!/usr/bin/env bash
# Reproduce a normal and an abnormal end-to-end run against the live HTTP
# service, keeping reviewable artifacts under runs/.
#
# Usage:
#   bash scripts/run_service_demo.sh
#
# Outputs:
#   runs/service-demo/<run_id>-ok.json     successful search response
#   runs/service-demo/<run_id>-error.json  structured error response
#   runs/service-demo/server.log           uvicorn log
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# shellcheck disable=SC1091
source .venv/bin/activate

OUT="runs/service-demo"
mkdir -p "$OUT"

python -m searchdsl.cli reindex --config config.json >/dev/null

HOST="127.0.0.1"
PORT="${PORT:-8021}"
BASE="http://${HOST}:${PORT}"

# Free a stale listener on the chosen port (from an interrupted prior run).
if command -v fuser >/dev/null 2>&1; then
  fuser -k "${PORT}/tcp" 2>/dev/null || true
fi

setsid python -m uvicorn searchdsl.service:app --host "$HOST" --port "$PORT" \
  --log-level warning >"$OUT/server.log" 2>&1 &
SERVER_PID=$!
trap 'kill -- -"$SERVER_PID" 2>/dev/null || kill "$SERVER_PID" 2>/dev/null || true' EXIT

# Wait for readiness (max ~10s).
for _ in $(seq 1 50); do
  if curl -fsS "$BASE/health" >/dev/null 2>&1; then break; fi
  sleep 0.2
done

RUN_ID="demo-$(date -u +%Y%m%dT%H%M%SZ)"
echo "run_id=$RUN_ID  base=$BASE"

# 1) Normal case: boolean + range + explain.
OK_Q='quick AND (fox OR salmon) AND year:[2000 TO 2020]'
ENCODED_OK=$(python -c 'import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))' "$OK_Q")
HTTP_OK=$(curl -sS -o "$OUT/${RUN_ID}-ok.json" -w '%{http_code}' \
  "$BASE/search?q=${ENCODED_OK}&explain=true")
echo "normal query:   HTTP $HTTP_OK"
python - "$OUT/${RUN_ID}-ok.json" <<'PY'
import json, sys
r = json.load(open(sys.argv[1]))
assert r["status"] == "ok", r
print("  run_id:", r["run_id"])
print("  total:", r["total"], "docs:", [d["doc_id"] for d in r["results"]])
print("  dsl_version:", r["versions"]["dsl_version"],
      "corpus:", r["versions"]["corpus_version"][:12])
print("  stages:", [e["stage"] for e in r["diagnostics"]["events"]])
PY

# 2) Abnormal case: unknown field must be HTTP 400 + FIELD_UNKNOWN.
ERR_Q='bogus:cat'
ENCODED_ERR=$(python -c 'import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))' "$ERR_Q")
HTTP_ERR=$(curl -sS -o "$OUT/${RUN_ID}-error.json" -w '%{http_code}' \
  "$BASE/search?q=${ENCODED_ERR}")
echo "abnormal query: HTTP $HTTP_ERR"
python - "$OUT/${RUN_ID}-error.json" <<'PY'
import json, sys
r = json.load(open(sys.argv[1]))
d = r["detail"]
assert d["code"] == "FIELD_UNKNOWN", r
assert d["pos"] == {"start": 0, "end": 9}, d
print("  code:", d["code"], "pos:", d["pos"])
PY
test "$HTTP_OK" = "200"
test "$HTTP_ERR" = "400"

echo "artifacts in $OUT/"
