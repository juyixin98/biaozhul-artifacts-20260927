#!/usr/bin/env bash
# End-to-end demo against a locally running se-server (see README for start command).
# Every request carries a request_id echoed in the response for log correlation.
set -euo pipefail

BASE="${SE_BASE:-http://127.0.0.1:8080}"
HERE="$(cd "$(dirname "$0")" && pwd)"

post() {
  local path="$1"; local file="$2"; shift 2
  curl -sS -X POST "$BASE$path" \
    -H 'Content-Type: application/json' \
    --data @"$file" "$@"
}

wrap_program() {
  python3 - "$HERE/programs/01_wrap_around.json" <<'PY'
import json, sys
prog = json.load(open(sys.argv[1]))
print(json.dumps({"request_id": "demo-wrap", "with_oracle": True,
                  "max_paths": 256, "max_loop_unroll": 64, "program": prog}))
PY
}

echo "== health =="
curl -sS "$BASE/health" | python3 -m json.tool

echo
echo "== analyze: u8 wrap-around (expect violation + confirmed witness + oracle) =="
wrap_program | curl -sS -X POST "$BASE/analyze" -H 'Content-Type: application/json' --data @- \
  | python3 -c '
import json, sys
r = json.load(sys.stdin)
print("request_id :", r["request_id"])
print("run_id     :", r["run_id"])
print("program_id :", r["program_id"])
print("verdict    :", r["verdict"])
v = r["replay"]["verified"][0]
print("witness    :", v["engine"]["inputs"], "replays as",
      v["replay_outcome"], "at stmt", v["replay_stmt"], "->", v["status"])
o = r["oracle"]
print("oracle     :", o["verdict"], "over", o["total_assignments"],
      "assignments,", len(o["failures"]), "failures")
print("budget     :", r["report"]["budget"])
'

echo
echo "== verify/replay: concrete run of a supplied input =="
python3 - "$HERE/programs/01_wrap_around.json" <<'PY' | post /verify/replay - | python3 -m json.tool
import json, sys
prog = json.load(open(sys.argv[1]))
print(json.dumps({"request_id": "demo-replay", "program": prog, "inputs": {"x": 156}}))
PY

echo
echo "== oracle only: small-domain ground truth =="
python3 - "$HERE/programs/02_mutex_paths.json" <<'PY' | post /oracle - | python3 -m json.tool
import json, sys
prog = json.load(open(sys.argv[1]))
print(json.dumps({"request_id": "demo-oracle", "program": prog, "cap": 4096}))
PY
