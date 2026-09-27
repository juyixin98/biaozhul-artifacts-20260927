#!/usr/bin/env python3
"""Generate the local synthetic WAV fixtures used by examples and README.

Outputs into examples/fixtures/:
  silence.wav         6 s digital silence (mono f32)
  constant_tone.wav   8 s 0.5 FS 1 kHz sine (mono f32)
  short_burst.wav     0.5 s tone inside 6 s of silence
  stereo.wav          8 s stereo (1 kHz L, 440 Hz R)
  five_one_lfe.wav    6 s 5.1 with loud LFE (must be ignored)
  dynamic_20s.wav     20 s two-level signal, LRA ~= 20 LU
"""

from __future__ import annotations

import os
import sys

import numpy as np
import scipy.io.wavfile

SR = 48000
OUT = os.path.join(os.path.dirname(__file__), "..", "examples", "fixtures")


def tone(level, freq, dur, phase=0.0):
    t = np.arange(int(dur * SR)) / SR
    return (level * np.sin(2 * np.pi * freq * t + phase)).astype(np.float32)


def main() -> None:
    os.makedirs(OUT, exist_ok=True)

    scipy.io.wavfile.write(os.path.join(OUT, "silence.wav"), SR,
                           np.zeros(int(6 * SR), dtype=np.float32))
    scipy.io.wavfile.write(os.path.join(OUT, "constant_tone.wav"), SR,
                           tone(0.5, 1000, 8))
    burst = np.zeros(int(6 * SR), dtype=np.float32)
    burst[:int(0.5 * SR)] = tone(0.5, 1000, 0.5)
    scipy.io.wavfile.write(os.path.join(OUT, "short_burst.wav"), SR, burst)

    t = np.arange(int(8 * SR)) / SR
    stereo = np.stack([0.5 * np.sin(2 * np.pi * 1000 * t),
                       0.3 * np.sin(2 * np.pi * 440 * t)], axis=1).astype(np.float32)
    scipy.io.wavfile.write(os.path.join(OUT, "stereo.wav"), SR, stereo)

    t6 = np.arange(int(6 * SR)) / SR
    fiveone = np.stack([
        0.3 * np.sin(2 * np.pi * 500 * t6),
        0.3 * np.sin(2 * np.pi * 500 * t6),
        0.25 * np.sin(2 * np.pi * 600 * t6),
        0.9 * np.sin(2 * np.pi * 80 * t6),   # LFE
        0.2 * np.sin(2 * np.pi * 700 * t6),
        0.2 * np.sin(2 * np.pi * 700 * t6),
    ], axis=1).astype(np.float32)
    scipy.io.wavfile.write(os.path.join(OUT, "five_one_lfe.wav"), SR, fiveone)

    n = int(20 * SR)
    t20 = np.arange(n) / SR
    level = np.where(t20 < 10, 0.2, 0.02)
    dyn = (level * np.sin(2 * np.pi * 300 * t20)).astype(np.float32)
    scipy.io.wavfile.write(os.path.join(OUT, "dynamic_20s.wav"), SR, dyn)

    print("wrote fixtures to", OUT)
    for f in sorted(os.listdir(OUT)):
        print(" -", f)


if __name__ == "__main__":
    sys.exit(main())
