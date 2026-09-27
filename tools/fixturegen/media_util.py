"""Numerical audio helpers for the *independent* fixture generator.

Nothing here imports the clockalign core. The shared-content mechanism lets
both virtual devices record exactly the same acoustic reality despite their
different clocks: a master noise field defined on a fine common grid (sample
rate ``master_fs``, sample centre ``(i + 0.5) / master_fs`` seconds of reality
time) is resampled at each device's own sample times.
"""
from __future__ import annotations

import wave

import numpy as np

_NOISE_MASTER: np.ndarray | None = None
_MASTER_FS: int = 16000


def set_noise_master(master: np.ndarray, fs: int = 16000) -> None:
    global _NOISE_MASTER, _MASTER_FS
    _NOISE_MASTER = np.asarray(master, dtype=np.float32)
    _MASTER_FS = int(fs)


def resample_master(t: np.ndarray) -> np.ndarray:
    """Linearly sample the shared reality noise field at times ``t``."""
    if _NOISE_MASTER is None:
        raise RuntimeError("noise master not set; call set_noise_master() first")
    pos = np.asarray(t, dtype=np.float64) * _MASTER_FS - 0.5
    i0 = np.floor(pos).astype(np.int64)
    frac = pos - i0
    n = _NOISE_MASTER.size
    out = np.zeros_like(pos, dtype=np.float32)
    valid = (i0 >= 0) & (i0 + 1 < n)
    lo = np.clip(i0, 0, n - 1)
    hi = np.clip(i0 + 1, 0, n - 1)
    sampled = _NOISE_MASTER[lo] * (1.0 - frac) + _NOISE_MASTER[hi] * frac
    out[valid] = sampled[valid].astype(np.float32)
    return out


def shared_content(t: np.ndarray, rng: np.random.Generator | None, *,
                   correlated: bool) -> np.ndarray:
    """Acoustic content at times ``t``.

    ``correlated=True``: deterministic periodic content plus the shared
    reality noise -- two devices really capture the same waveform.
    ``correlated=False``: independent white noise -- nothing alignable.
    """
    if not correlated:
        if rng is None:
            raise RuntimeError("need an rng for uncorrelated content")
        return (0.3 * rng.standard_normal(t.shape)).astype(np.float32)
    sine = (0.25 * np.sin(2 * np.pi * 220.0 * t)
            + 0.15 * np.sin(2 * np.pi * 330.0 * t + 0.7)).astype(np.float32)
    noise = resample_master(t) * 0.12
    return (sine + noise).astype(np.float32)


def hann_burst(t0: float, freq_hz: float, duration_s: float, fs: int,
               amplitude: float = 0.6) -> tuple[np.ndarray, np.ndarray]:
    # Convention shared with the detector: the burst starts at the sample
    # whose index is round(t0*fs); sample m of the burst has phase at m/fs.
    n = int(round(duration_s * fs))
    tt = np.arange(n) / fs
    burst = amplitude * np.hanning(n) * np.sin(2 * np.pi * freq_hz * tt)
    return tt + t0, burst.astype(np.float32)


def add_burst_at(field: np.ndarray, t_grid: np.ndarray, t_event: float,
                 freq_hz: float, duration_s: float, fs: int,
                 amplitude: float = 0.6) -> np.ndarray:
    """Add a pulse whose onset sample is at round(t_event*fs).

    ``t_grid`` is accepted for signature symmetry; onset placement uses the
    shared index convention, not the sample-centre values in the grid.
    """
    times, burst = hann_burst(t_event, freq_hz, duration_s, fs, amplitude)
    idx = np.round(times * fs).astype(np.int64)
    valid = (idx >= 0) & (idx < field.size)
    out = field.copy()
    np.add.at(out, idx[valid], burst[valid])
    return out


def mix_pulse(field: np.ndarray, t_grid: np.ndarray, t_event: float,
              freq_hz: float, duration_s: float, fs: int, *,
              amplitude: float = 0.6, time_grid: np.ndarray | None = None
              ) -> np.ndarray:
    """Place a pulse occurring at grid time ``t_event``.

    By default ``t_event`` is interpreted on the same time array the field was
    built on. ``time_grid`` lets the caller stamp a slave-local pulse
    explicitly on the slave clock.
    """
    return add_burst_at(field, time_grid if time_grid is not None else t_grid,
                        t_event, freq_hz, duration_s, fs, amplitude)


def linear_interp(x: np.ndarray, positions: np.ndarray) -> np.ndarray:
    """Public linear interpolation (used by tests, not by the core)."""
    pos = np.asarray(positions, dtype=np.float64)
    i0 = np.floor(pos).astype(np.int64)
    frac = pos - i0
    n = x.size
    lo = np.clip(i0, 0, n - 1)
    hi = np.clip(i0 + 1, 0, n - 1)
    return x[lo] * (1.0 - frac) + x[hi] * frac


def write_stereo_wav(path, ref: np.ndarray, slave: np.ndarray, fs: int) -> None:
    n = min(ref.size, slave.size)
    stereo = np.empty((n, 2), dtype=np.float32)
    stereo[:, 0] = ref[:n]
    stereo[:, 1] = slave[:n]
    pcm = (np.clip(stereo, -1.0, 1.0) * 32767.0).round().astype("<i2")
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(int(fs))
        wf.writeframes(pcm.reshape(-1).tobytes())
