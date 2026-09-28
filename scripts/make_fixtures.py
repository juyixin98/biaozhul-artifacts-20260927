"""生成本地合成 WAV 夹具（无需任何外部数据）。

夹具设计（与 tests/test_kernel_hand_verified.py 的手算区间一一对应）:

1. near_threshold_pulses.wav  sr=1000, 1.20s
   双阈值中间带与短噪声行为:
   [0,200)    speech 0.5
   [200,300)  silence 0.00           (100 >= min_silence 100)
   [300,330)  0.05 中间带脉冲        (30 < min_speech 50 -> 短噪声)
   [330,500)  0.00                   (170 静音)
   [500,580)  0.5 speech
   [580,700)  0.00
   [700,740)  0.05 中间带脉冲 (40 样本短噪声)
   [740,900)  0.00
   [900,1000) 0.5 speech (100)
   [1000,1200) 0.00 尾部静音
   注：所有脉冲从 0 起跳变；0.05 处于 enter=0.03 与 exit=0.08 之间。

2. cross_chunk_silence.wav  sr=1000, 1.20s
   speech 200 + 400 silence + speech 200 + 400 silence，用于跨块切分演示。

3. all_silence.wav sr=1000, 0.5s 全 0。

4. tail_unfinished.wav sr=1000, 0.75s
   400 silence 后接 350 高电平（>= min_speech），无收尾静音。
"""
from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.media import wav_bytes  # noqa: E402

SR = 1000
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "data", "fixtures")


def _sig_pulses() -> np.ndarray:
    x = np.zeros(1200)
    x[0:200] = 0.5
    x[300:330] = 0.05
    x[500:580] = 0.5
    x[700:740] = 0.05
    x[900:1000] = 0.5
    return x


def _sig_cross_chunk() -> np.ndarray:
    x = np.zeros(1200)
    x[0:200] = 0.5
    x[600:800] = 0.5
    return x


def _sig_tail() -> np.ndarray:
    x = np.zeros(750)
    x[400:750] = 0.5
    return x


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    fixtures = {
        "near_threshold_pulses.wav": _sig_pulses(),
        "cross_chunk_silence.wav": _sig_cross_chunk(),
        "all_silence.wav": np.zeros(500),
        "tail_unfinished.wav": _sig_tail(),
    }
    for name, x in fixtures.items():
        path = os.path.join(OUT, name)
        with open(path, "wb") as f:
            f.write(wav_bytes(x, SR))
        print(f"wrote {path} ({len(x)} samples @ {SR}Hz)")


if __name__ == "__main__":
    main()
