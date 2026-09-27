"""Shared synthetic fixtures and measurement helpers.

All signals are generated locally; no production audio or accounts are used.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

import numpy as np
import pytest

from app.config import get_settings
from app.media import DecodedAudio
from app.service import measure

SR = 48000


@dataclass
class Sig:
    samples: np.ndarray
    weights: tuple[float, ...]
    layout: str
    source_channels: int


def sine(level: float, freq: float, dur_sec: float, sr: int = SR,
         phase: float = 0.0) -> np.ndarray:
    t = np.arange(int(round(dur_sec * sr))) / sr
    return level * np.sin(2 * np.pi * freq * t + phase)


@pytest.fixture
def settings():
    return get_settings()


@pytest.fixture
def silence_sig():
    dur = 6.0
    return Sig(np.zeros((int(dur * SR), 1), dtype=np.float64), (1.0,), "mono", 1)


@pytest.fixture
def constant_sig():
    dur = 8.0
    x = sine(0.5, 1000.0, dur)[:, None]
    return Sig(x, (1.0,), "mono", 1)


@pytest.fixture
def stereo_constant_sig():
    dur = 8.0
    t = np.arange(int(dur * SR)) / SR
    l = 0.5 * np.sin(2 * np.pi * 1000 * t)
    r = 0.3 * np.sin(2 * np.pi * 440 * t)
    return Sig(np.stack([l, r], axis=1), (1.0, 1.0), "stereo", 2)


@pytest.fixture
def short_burst_sig():
    """0.5 s tone inside 6 s of silence: exercises relative gating."""
    dur = 6.0
    x = np.zeros((int(dur * SR), 1), dtype=np.float64)
    n = int(0.5 * SR)
    x[:n, 0] = sine(0.5, 1000.0, 0.5)
    return Sig(x, (1.0,), "mono", 1)


@pytest.fixture
def too_short_sig():
    """200 ms: not one complete 400 ms momentary block."""
    x = sine(0.5, 1000.0, 0.2)[:, None]
    return Sig(x, (1.0,), "mono", 1)


@pytest.fixture
def dynamic_sig():
    """Two-level 8 s signal: 4 s loud then 4 s quiet (~20 LU apart)."""
    dur = 8.0
    t = np.arange(int(dur * SR)) / SR
    level = np.where(t < dur / 2, 0.2, 0.02)
    x = (level * np.sin(2 * np.pi * 300 * t))[:, None]
    return Sig(x.astype(np.float64), (1.0,), "mono", 1)


@pytest.fixture
def dynamic_long_sig():
    """20 s two-level signal for a confident LRA (>=30 short-term blocks)."""
    dur = 20.0
    t = np.arange(int(dur * SR)) / SR
    level = np.where(t < dur / 2, 0.2, 0.02)
    x = (level * np.sin(2 * np.pi * 300 * t))[:, None]
    return Sig(x.astype(np.float64), (1.0,), "mono", 1)


@pytest.fixture
def channel_change_sig():
    """5.1 material reduced to the 5 R128 analysis channels (LFE already
    removed, as the media layer does). A separate 6-channel raw fixture is
    built directly in the media tests."""
    dur = 8.0
    t = np.arange(int(dur * SR)) / SR
    tone_lr = 0.3 * np.sin(2 * np.pi * 500 * t)
    center = 0.25 * np.sin(2 * np.pi * 600 * t)
    surr = 0.2 * np.sin(2 * np.pi * 700 * t)
    # [L, R, C, Ls, Rs]; the loud LFE at 80 Hz is excluded by construction.
    x = np.stack([tone_lr, tone_lr, center, surr, surr], axis=1)
    weights = (1.0, 1.0, 1.0, 1.41, 1.41)
    return Sig(x.astype(np.float64), weights, "5.1", 6)


@pytest.fixture
def five_one_with_loud_lfe():
    """Raw 6-channel WAV signal with a loud LFE that must be dropped."""
    dur = 8.0
    t = np.arange(int(dur * SR)) / SR
    quiet = np.zeros_like(t)
    lfe = 0.9 * np.sin(2 * np.pi * 80 * t)
    x = np.stack([quiet, quiet, quiet, lfe, quiet, quiet], axis=1)
    weights = (1.0, 1.0, 1.0, 1.41, 1.41)
    return Sig(x.astype(np.float64), weights, "5.1", 6)


def to_decoded(sig: Sig, sr: int = SR) -> DecodedAudio:
    return DecodedAudio(
        samples=sig.samples.astype(np.float64),
        sample_rate=sr,
        layout=sig.layout,
        channel_weights=sig.weights,
        source_channels=sig.source_channels,
        source_format="fixture:f32",
    )


def run_measure(sig: Sig, settings, *, include_blocks=True, chunk_samples=None,
                request_id="fixture", sr: int = SR) -> dict:
    return measure(to_decoded(sig, sr), request_id=request_id,
                   settings=settings, include_blocks=include_blocks,
                   chunk_samples=chunk_samples)


# -- minimal WAV writer (independent of the parser under test) -------------

def write_wav_bytes(samples_f32: np.ndarray, sr: int, *,
                    audio_format: int = 3, bits_per_sample: int = 32) -> bytes:
    """Write an IEEE-float (tag 3) or PCM (tag 1) WAV, interleaved frames."""
    if samples_f32.ndim == 1:
        samples_f32 = samples_f32[:, None]
    n_ch = samples_f32.shape[1]
    if audio_format == 3:
        payload = samples_f32.astype("<f4").tobytes()
        block_align = n_ch * 4
        byte_rate = sr * block_align
    else:
        raise NotImplementedError("use write_pcm_wav_bytes for integer PCM")
    fmt_chunk = struct.pack("<HHIIHH", audio_format, n_ch, sr, byte_rate,
                            block_align, bits_per_sample)
    out = b"RIFF"
    out += struct.pack("<I", 4 + (8 + len(fmt_chunk)) + (8 + len(payload)))
    out += b"WAVE"
    out += b"fmt " + struct.pack("<I", len(fmt_chunk)) + fmt_chunk
    out += b"data" + struct.pack("<I", len(payload)) + payload
    return out


def write_pcm_wav_bytes(samples: np.ndarray, sr: int, bits: int) -> bytes:
    """Write integer PCM WAV (8/16/24/32-bit) from float samples in [-1,1]."""
    if samples.ndim == 1:
        samples = samples[:, None]
    n_ch = samples.shape[1]
    clipped = np.clip(samples, -1.0, 1.0)
    if bits == 16:
        payload = (clipped * 32767).astype("<i2").tobytes()
    elif bits == 32:
        payload = (clipped * 2147483647).astype("<i4").tobytes()
    elif bits == 8:
        payload = np.round(clipped * 127 + 128).clip(0, 255).astype(np.uint8).tobytes()
    elif bits == 24:
        scaled = np.round(clipped * 8388607).astype(np.int32)
        b0 = scaled & 0xFF
        b1 = (scaled >> 8) & 0xFF
        b2 = (scaled >> 16) & 0xFF
        payload = np.stack([b0, b1, b2], axis=-1).astype(np.uint8).tobytes()
    else:
        raise ValueError(bits)
    block_align = n_ch * bits // 8
    byte_rate = sr * block_align
    fmt_chunk = struct.pack("<HHIIHH", 1, n_ch, sr, byte_rate, block_align, bits)
    out = b"RIFF"
    out += struct.pack("<I", 4 + (8 + len(fmt_chunk)) + (8 + len(payload)))
    out += b"WAVE"
    out += b"fmt " + struct.pack("<I", len(fmt_chunk)) + fmt_chunk
    out += b"data" + struct.pack("<I", len(payload)) + payload
    return out
