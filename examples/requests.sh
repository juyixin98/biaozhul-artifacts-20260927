#!/usr/bin/env bash
# Example requests against a locally running server.
# Start first:  .venv/bin/python -m app.main   (listens on 127.0.0.1:8000)
# Generate fixtures: .venv/bin/python scripts/generate_fixtures.py
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8000}"
FX="examples/fixtures"

echo "1) health / version"
curl -s "$BASE/health" -H 'X-Request-Id: demo-health' | python3 -m json.tool
curl -s "$BASE/version" | python3 -m json.tool

echo
echo "2) constant tone (sync WAV) -> integrated ~ -9.07 LUFS, LRA 0"
curl -s -X POST "$BASE/measurements/wav" \
  -F "payload=@$FX/constant_tone.wav;type=audio/wav" \
  -F "include_blocks=true" -F "label=constant-1khz" \
  -H 'X-Request-Id: demo-constant' | python3 -m json.tool

echo
echo "3) silence -> status SILENCE, no LUFS number"
curl -s -X POST "$BASE/measurements/wav" \
  -F "payload=@$FX/silence.wav;type=audio/wav" \
  -H 'X-Request-Id: demo-silence' | python3 -m json.tool

echo
echo "4) short burst -> gating block counts in gate_stats"
curl -s -X POST "$BASE/measurements/wav" \
  -F "payload=@$FX/short_burst.wav;type=audio/wav" \
  -F "include_blocks=true" \
  -H 'X-Request-Id: demo-burst' | python3 -m json.tool

echo
echo "5) 5.1 with loud LFE -> LFE excluded, 5 analysis channels"
curl -s -X POST "$BASE/measurements/wav" \
  -F "payload=@$FX/five_one_lfe.wav;type=audio/wav" \
  -H 'X-Request-Id: demo-lfe' | python3 -m json.tool

echo
echo "6) raw headerless s16le mono PCM"
ffmpeg -loglevel error -y -i "$FX/constant_tone.wav" -f s16le -ac 1 -ar 48000 /tmp/tone.s16
curl -s -X POST "$BASE/measurements/pcm" \
  -F "payload=@/tmp/tone.s16;type=application/octet-stream" \
  -F "sample_rate=48000" -F "channels=1" -F "sample_format=s16" \
  -H 'X-Request-Id: demo-rawpcm' | python3 -m json.tool

echo
echo "7) async job: create then fetch"
JOB=$(curl -s -X POST "$BASE/jobs" -F "payload=@$FX/dynamic_20s.wav;type=audio/wav")
echo "$JOB" | python3 -m json.tool
JOB_ID=$(echo "$JOB" | python3 -c 'import sys,json; print(json.load(sys.stdin)["job_id"])')
curl -s "$BASE/jobs/$JOB_ID" -H 'X-Request-Id: demo-job' | python3 -m json.tool

echo
echo "8) invalid: compressed/garbage file -> explicit failure code"
curl -s -X POST "$BASE/measurements/wav" \
  -F "payload=@$FX/constant_tone.wav;filename=x.mp3;type=audio/mpeg" \
  -H 'X-Request-Id: demo-bad' | python3 -m json.tool || true
# (the bytes are still a WAV; this demonstrates the response envelope — feed a
# real MP3 and the code will be WAV_NOT_RIFF / WAV_COMPRESSED_UNSUPPORTED)
