#!/usr/bin/env bash
# End-to-end local verification against a freshly started service.
# Usage: scripts/demo.sh [base_url]
set -euo pipefail

BASE="${1:-http://127.0.0.1:8080}"
export PYTHONPATH=src
PY=.venv/bin/python

echo "== health =="
curl -fsS "$BASE/health"; echo

echo "== validate 48k -> 16k =="
curl -fsS -X POST "$BASE/resample/validate" -H 'Content-Type: application/json' \
    -d '{"input_rate":48000,"output_rate":16000}' | $PY -m json.tool | head -20

echo "== generate fixtures =="
mkdir -p demo data
$PY - <<'PYEOF'
import numpy as np
from resampler.media import build_wav
n = 48000 * 2
t = np.arange(n) / 48000
x = 0.5*np.sin(2*np.pi*1000*t) + 0.5*np.sin(2*np.pi*12000*t)
open("demo/in48k_mix.wav", "wb").write(build_wav(x, 48000, "s16le"))
xr = (0.6*np.sin(2*np.pi*700*np.arange(24000)/8000.0)).astype("<f8")
xr.tofile("demo/in8k.f64")
print("fixtures ready")
PYEOF

$PY -m resampler.cli resample-wav --base-url "$BASE" \
    --input demo/in48k_mix.wav --input-rate 48000 --output-rate 16000 \
    --output demo/out16k.wav
$PY -m resampler.cli resample-raw --base-url "$BASE" \
    --input demo/in8k.f64 --input-rate 8000 --output-rate 12000 \
    --chunk-samples 37 --output demo/out12k.f64

echo "== verify alias suppression + chunk invariance =="
$PY - <<'PYEOF'
import numpy as np
from resampler.media import parse_wav
from resampler.signal import StreamingPolyphase, build_plan

d = parse_wav(open("demo/out16k.wav","rb").read())
y = d.samples[400:-400]
def amp(x, fs, f):
    m = (len(x)//int(fs/f))*int(fs/f)
    x = x[:m]
    return 2*abs(np.dot(x, np.exp(-2j*np.pi*f*np.arange(m)/fs)))/m
g1k = 20*np.log10(amp(y,16000,1000)/0.5)
g4k = 20*np.log10((amp(y,16000,4000)+1e-12)/0.5)
print(f"1 kHz gain {g1k:.2f} dB; 12 kHz alias@4 kHz {g4k:.1f} dB")
assert abs(g1k) < 0.05 and g4k <= -60

x = np.fromfile("demo/in8k.f64", dtype="<f8")
got = np.fromfile("demo/out12k.f64", dtype="<f8")
plan = build_plan(8000, 12000)
eng = StreamingPolyphase(plan)
ref = np.concatenate([eng.push(x), eng.flush()])
assert np.array_equal(got, ref) and got.size == plan.expected_outputs(x.size)
print("chunk=37 output bit-identical to one-shot; count", got.size)
print("DEMO OK")
PYEOF
