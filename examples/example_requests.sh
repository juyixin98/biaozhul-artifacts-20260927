#!/usr/bin/env bash
# Local smoke test against a running server (start with scripts/run_local.sh).
#
#   scripts/run_local.sh            # in one terminal
#   examples/example_requests.sh    # in another
set -euo pipefail
BASE="${BASE:-http://127.0.0.1:8000}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/.venv/bin/python"

echo "### 1. health / version / processing location"
curl -s "$BASE/health" -H 'X-Request-ID: demo-health' | "$PY" -m json.tool

echo
echo "### 2. build local fixtures (5 s -23 LUFS tone, 2 s silence)"
( cd "$ROOT" && "$PY" - <<'PY'
from tests import fixtures as fx
open("/tmp/demo_tone.wav", "wb").write(fx.to_wav(
    fx.calibrated_loudness_tone(5.0, -23.0, 1)))
open("/tmp/demo_silence.wav", "wb").write(fx.to_wav(fx.digital_silence(2.0)))
print("wrote /tmp/demo_tone.wav and /tmp/demo_silence.wav")
PY
)

echo
echo "### 3. one-shot WAV analysis (expect I ~ -23.0 LUFS)"
curl -s -X POST "$BASE/analyze/wav" \
    -H 'Content-Type: application/octet-stream' \
    -H 'X-Request-ID: demo-wav-001' \
    --data-binary @/tmp/demo_tone.wav | "$PY" -m json.tool

echo
echo "### 4. chunked raw s16 stereo job, deliberately misaligned chunks"
JOB=$(curl -s -X POST "$BASE/jobs" \
    -H 'Content-Type: application/json' \
    -H 'X-Request-ID: demo-job-001' \
    -d '{"channels": 2, "sample_format": "s16"}' \
    | "$PY" -c 'import sys,json;print(json.load(sys.stdin)["job_id"])')
echo "job=$JOB"
( cd "$ROOT" && BASE="$BASE" "$PY" - "$JOB" <<'PY'
import os, sys, urllib.request
from tests import fixtures as fx
job = sys.argv[1]
base = os.environ["BASE"]
pcm = (fx.calibrated_loudness_tone(4.3, -23.0, 2) * 32768).clip(
    -32768, 32767).astype("<i2").tobytes()
for a in range(0, len(pcm), 9973):  # 9973 is odd -> crosses 4-byte frames
    req = urllib.request.Request(
        f"{base}/jobs/{job}/chunks",
        data=pcm[a:a + 9973], method="POST")
    urllib.request.urlopen(req).read()
print("chunks uploaded")
PY
)
curl -s -X POST "$BASE/jobs/$JOB/finalize" \
    -H 'X-Request-ID: demo-job-001' | "$PY" -m json.tool

echo
echo "### 5. silence returns a dedicated status (never a fabricated number)"
curl -s -X POST "$BASE/analyze/wav" \
    -H 'Content-Type: application/octet-stream' \
    --data-binary @/tmp/demo_silence.wav | "$PY" -m json.tool \
    | grep -E '"status"|"integrated_loudness_lufs"'
