"""本地合成夹具：生成四种关键场景的单声道 s16 WAV 文件。

  * threshold_pulse.wav —— 阈值附近脉冲 + 长静音中的短噪声
  * long_silence.wav    —— 跨块长静音（两段声音，中间 0.6s 静音）
  * all_silence.wav     —— 全静音
  * trailing.wav        —— 末尾未完成段（声音后不足最小静音即结束）

全部为本地合成，无外部数据。默认输出到 ./samples。
"""

from __future__ import annotations

import argparse
import io
import wave
from pathlib import Path

import numpy as np

SR = 1000
LOUD = 0.5
MID = 0.03
LOW = 0.0


def _wav(path: Path, values: np.ndarray, rate: int = SR) -> None:
    pcm = np.clip(values, -1.0, 1.0)
    pcm = (pcm * 32767.0).round().astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())


def build() -> dict[str, np.ndarray]:
    return {
        # 200 低 + 5 中 + 120 响 + 5 中 + 300 低 + 10 响(短噪声) + 430 低
        "threshold_pulse": np.array(
            [LOW] * 200 + [MID] * 5 + [LOUD] * 120 + [MID] * 5
            + [LOW] * 300 + [LOUD] * 10 + [LOW] * 430
        ),
        "long_silence": np.array(
            [LOUD] * 100 + [LOW] * 600 + [LOUD] * 100
        ),
        "all_silence": np.zeros(800),
        # 200 低 + 100 响 + 100 低（不足 min_silence=300）
        "trailing": np.array([LOW] * 200 + [LOUD] * 100 + [LOW] * 100),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="samples")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for name, sig in build().items():
        _wav(out / f"{name}.wav", sig)
        print(f"wrote {out / (name + '.wav')}  ({len(sig)} samples @ {SR}Hz)")


if __name__ == "__main__":
    main()
