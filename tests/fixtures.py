"""Deterministic synthetic PCM fixtures.

All fixtures are generated locally (no recordings, no network): calibrated
sine tones, filtered noise, bursts, channel-layout cases and edge cases. They
return float64 arrays at the normative 48 kHz, and a WAV encoder provides
serialised containers for the API and for ffmpeg.
"""
from __future__ import annotations

import math

import numpy as np

FS = 48_000


def time_axis(seconds: float) -> np.ndarray:
    return np.arange(int(round(seconds * FS))) / FS


def sine(seconds: float, freq_hz: float, lufs: float,
         channels: int = 1, phase: float = 0.0) -> np.ndarray:
    """Sine at a target *unweighted* level.

    A sine at ``P`` dBFS has RMS P - 3.01 dBFS; K-weighting adds
    10*log10(1 + 1/(2*Q^2)) style ripple, so for cross-tool work we calibrate
    empirically via :func:`calibrated_loudness_tone` instead. Here ``lufs`` is
    interpreted as target RMS-ish level only for simple tests; prefer the
    calibrated builder for exact LUFS assertions.
    """
    amp = 10.0 ** (lufs / 20.0)
    x = amp * np.sin(2 * np.pi * freq_hz * time_axis(seconds) + phase)
    return _to_channels(x, channels)


def calibrated_loudness_tone(seconds: float, lufs: float = -23.0,
                             channels: int = 1,
                             freq_hz: float = 1000.0) -> np.ndarray:
    """Tone whose gated loudness equals ``lufs`` LUFS (calibrated by reference).

    Calibration constant (-23 dBFS RMS => -23 LUFS measured for a 1 kHz tone at
    48 kHz) is the BS.1770 design point and is itself re-checked against
    ffmpeg in the oracle tests, not merely assumed.
    """
    # Full-scale sine: peak 1.0 -> RMS 1/sqrt(2). Scale so RMS = 10**(lufs/20).
    amp = math.sqrt(2.0) * (10.0 ** (lufs / 20.0))
    x = amp * np.sin(2 * np.pi * freq_hz * time_axis(seconds))
    return _to_channels(x, channels, identical=True)


def _to_channels(x: np.ndarray, channels: int, identical: bool = False) -> np.ndarray:
    if channels == 1:
        return x[:, None]
    if identical:
        return np.stack([x] * channels, axis=1)
    # decorrelated by small deterministic phases
    out = [x]
    for c in range(1, channels):
        n = np.roll(x, c * 137)
        out.append(n)
    return np.stack(out, axis=1)


def digital_silence(seconds: float, channels: int = 1) -> np.ndarray:
    return np.zeros((int(seconds * FS), channels), dtype=np.float64)


def tone_burst(seconds_total: float, start: float, duration: float,
               lufs: float = -23.0, freq_hz: float = 1000.0,
               channels: int = 1) -> np.ndarray:
    total = int(seconds_total * FS)
    out = np.zeros((total, channels), dtype=np.float64)
    tone = calibrated_loudness_tone(duration, lufs, channels=1,
                                    freq_hz=freq_hz)[:, 0]
    s = int(start * FS)
    e = min(s + len(tone), total)
    tone = tone[: e - s]
    for c in range(channels):
        out[s:e, c] = tone
    return out


def quiet_below_abs_gate(seconds: float, lufs: float = -80.0,
                         channels: int = 1) -> np.ndarray:
    """Non-silent signal whose every block is below the -70 LUFS gate."""
    amp = math.sqrt(2.0) * (10.0 ** (lufs / 20.0))
    rng = np.random.default_rng(7)
    x = amp * rng.standard_normal(int(seconds * FS))
    return _to_channels(x, channels, identical=False)[: int(seconds * FS)]


def bandpassed_noise(seconds: float, rms_lufs: float, seed: int,
                     channels: int = 1) -> np.ndarray:
    """Pink-ish noise (simple one-pole-ish smoothing) at a target RMS level."""
    rng = np.random.default_rng(seed)
    n = int(seconds * FS)
    x = rng.standard_normal(n)
    # crude low-pass to shape toward programme-like spectrum
    y = np.empty(n)
    acc = 0.0
    for i in range(n):  # short signals only (<= a few s of segments)
        acc = 0.85 * acc + 0.15 * x[i]
        y[i] = acc
    y /= np.sqrt(np.mean(y * y))
    y *= 10.0 ** (rms_lufs / 20.0)
    return _to_channels(y, channels, identical=False)[:n]


def segmented_programme(segment_specs: list[tuple[float, float]],
                        gap_seconds: float = 2.0, seed0: int = 100,
                        channels: int = 1) -> np.ndarray:
    """Concatenate (seconds, level_LUFS_rms) noise segments with silence gaps."""
    parts: list[np.ndarray] = []
    seed = seed0
    for dur, level in segment_specs:
        parts.append(bandpassed_noise(dur, level, seed=seed, channels=channels))
        seed += 1
        if gap_seconds:
            parts.append(digital_silence(gap_seconds, channels))
    return np.concatenate(parts, axis=0)


def surround_mix(seconds: float, level_lufs: float = -23.0) -> np.ndarray:
    """6-channel (L R C LFE Ls Rs) signal.

    Bed in L/R/C at one level, an identical bed in Ls/Rs at the same *sample*
    level so the +1.5 dB surround weight is the only difference to test, and a
    separate inaudible-to-loudness tone in the LFE channel (must be ignored).
    """
    base = calibrated_loudness_tone(seconds, level_lufs, channels=1)[:, 0]
    lfe = np.clip(
        0.9 * np.sin(2 * np.pi * 60.0 * time_axis(seconds)), -1, 1
    )
    return np.stack([base, base, base, lfe, base, base], axis=1)


def channel_switch(seconds: float) -> np.ndarray:
    """Stereo: signal only on L for first half, only on R for second half."""
    half = int(seconds * FS) // 2
    a = calibrated_loudness_tone(half / FS, -23.0, channels=1)[:, 0]
    out = np.zeros((half * 2, 2))
    out[:half, 0] = a
    out[half:, 1] = a
    return out


# ---------------------------------------------------------------------------
# WAV encoding
# ---------------------------------------------------------------------------
def to_wav(samples: np.ndarray, sample_format: str = "s16",
           sample_rate: int = FS) -> bytes:
    """Serialise float64 (frames, ch) samples to a WAV byte string.

    Built by hand (not the ``wave`` module) so that float samples carry the
    IEEE_FLOAT format tag (0x0003) and 64-bit width is supported.
    """
    import struct
    if samples.ndim == 1:
        samples = samples[:, None]
    ch = samples.shape[1]
    if sample_format == "s16":
        sw, tag, data = 2, 0x0001, _pcm16(samples)
    elif sample_format == "s24":
        sw, tag, data = 3, 0x0001, _pcm24(samples)
    elif sample_format == "s32":
        sw, tag, data = 4, 0x0001, _pcm32(samples)
    elif sample_format == "f32":
        sw, tag, data = 4, 0x0003, np.asarray(samples, dtype="<f4").tobytes()
    elif sample_format == "f64":
        sw, tag, data = 8, 0x0003, np.asarray(samples, dtype="<f8").tobytes()
    else:
        raise ValueError(sample_format)
    block_align = sw * ch
    byte_rate = sample_rate * block_align
    fmt = struct.pack("<HHIIHH", tag, ch, sample_rate, byte_rate,
                      block_align, sw * 8)
    out = b"fmt " + struct.pack("<I", len(fmt)) + fmt
    out += b"data" + struct.pack("<I", len(data)) + data
    riff = b"RIFF" + struct.pack("<I", 4 + len(out)) + b"WAVE" + out
    return riff


def _pcm16(s: np.ndarray) -> bytes:
    x = np.clip(s, -1.0, 1.0)
    # symmetric -32768..+32767 scaling, matching the decoder and BS.1770 practice
    return (x * 32768.0).clip(-32768, 32767).astype("<i2").reshape(-1).tobytes()


def _pcm24(s: np.ndarray) -> bytes:
    x = np.clip(s, -1.0, 1.0)
    v = (x * 8388608.0).clip(-8388608, 8388607).astype(np.int32).reshape(-1)
    out = np.empty((v.size, 3), dtype=np.uint8)
    out[:, 0] = v & 0xFF
    out[:, 1] = (v >> 8) & 0xFF
    out[:, 2] = (v >> 16) & 0xFF
    return out.tobytes()


def _pcm32(s: np.ndarray) -> bytes:
    x = np.clip(s, -1.0, 1.0)
    return (x * 2147483648.0).clip(-2147483648, 2147483647).astype("<i4").reshape(-1).tobytes()
