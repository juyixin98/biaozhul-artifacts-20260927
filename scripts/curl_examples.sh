#!/usr/bin/env bash
# Plain-curl service call examples.  Start scripts/run_server.sh first.
set -euo pipefail
BASE="${APP_BASE:-http://127.0.0.1:8000}"

echo "== health/version =="
curl -s "$BASE/health"; echo
curl -s "$BASE/version"; echo

echo "== direct concat (normal) =="
JOB=$(curl -s -X POST "$BASE/jobs" -H 'content-type: application/json' -d '{
  "segments": [{"path": "seg_ok_a.json"}, {"path": "seg_ok_b.json"}]}')
echo "$JOB" | .venv/bin/python -m json.tool
JOB_ID=$(echo "$JOB" | .venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["job_id"])')

echo "== per-sample plan (first video samples) =="
curl -s "$BASE/jobs/$JOB_ID/plan" \
  | .venv/bin/python -c 'import json,sys; p=json.load(sys.stdin); print(json.dumps(p["tracks"][0]["samples"][:4], indent=2))'

echo "== independent re-validation =="
curl -s -X POST "$BASE/jobs/$JOB_ID/validate"; echo

echo "== abnormal: time-base mismatch -> transcode_required =="
curl -s -X POST "$BASE/jobs" -H 'content-type: application/json' -d '{
  "segments": [{"path": "seg_ok_a.json"}, {"path": "seg_tb_mismatch.json"}]}' \
  | .venv/bin/python -m json.tool

echo "== abnormal: dangling reference -> failed/MISSING_REFERENCE =="
curl -s -X POST "$BASE/jobs" -H 'content-type: application/json' -d '{
  "segments": [{"path": "seg_dangling_ref.json"}]}' \
  | .venv/bin/python -m json.tool
