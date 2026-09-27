"""WAV reading/writing built on the stdlib `wave` module (PCM 16-bit / 32-bit float).

Kept dependency-free on purpose: the fixtures are mono PCM, and pulling in
libsndfile for that would be needless weight. Multi-channel files are
downmixed to mono on read.
"""

from __future__ import annotations

import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np


class MediaDecodeError(Exception):
    """Raised when an audio file cannot be decoded."""


@dataclass(frozen=True)
class AudioData:
    samples: np.ndarray  # float64 mono, roughly [-1, 1]
    sample_rate: int
    source_path: str

    @property
    def duration_s(self) -> float:
        return len(self.samples) / self.sample_rate


def read_wav(path: str | Path) -> AudioData:
    path = Path(path)
    if not path.exists():
        raise MediaDecodeError(f"audio file not found: {path}")
    try:
        with wave.open(str(path), "rb") as wf:
            channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            fs = wf.getframerate()
            raw = wf.readframes(wf.getnframes())
    except (wave.Error, EOFError, OSError) as exc:
        raise MediaDecodeError(f"cannot decode WAV {path}: {exc}") from exc

    if sampwidth == 2:
        data = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
    elif sampwidth == 4:
        data = np.frombuffer(raw, dtype="<i4").astype(np.float64) / 2147483648.0
    else:
        raise MediaDecodeError(f"unsupported sample width {sampwidth} bytes in {path}")

    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return AudioData(samples=np.ascontiguousarray(data), sample_rate=fs,
                     source_path=str(path))


def write_wav(path: str | Path, samples: np.ndarray, sample_rate: int) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = np.clip(samples, -1.0, 1.0)
    pcm16 = (pcm * 32767.0).round().astype("<i2")
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm16.tobytes())
