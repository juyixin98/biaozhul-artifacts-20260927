"""Resampling correction: windowed-sinc interpolation onto the reference grid.

This module only changes the *rate/phase* of the audio. Mapping of metadata
timestamps is a separate concern (core.timeline) and is reported separately.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class CorrectedAudio:
    samples: np.ndarray
    sample_rate: int
    # Where output sample n sits on the *target* recording's sample grid:
    #   target_position(n) = first_target_position + (1 + drift) * n
    first_target_position: float
    drift_ratio: float
    half_width_taps: int


def windowed_sinc_at(x: np.ndarray, positions: np.ndarray,
                     half_width: int = 16) -> np.ndarray:
    """Evaluate the band-limited interpolation of `x` at float `positions`.

    positions must lie within [half_width - 1, len(x) - half_width]; callers
    are expected to clip their output range accordingly (see correct_clock).
    """
    x = np.asarray(x, dtype=np.float64)
    positions = np.asarray(positions, dtype=np.float64)
    k = np.arange(-half_width + 1, half_width + 1)  # 2*half_width taps
    base = np.floor(positions)[:, None]
    grid = base + k[None, :]                        # (M, 2W) integer indices
    d = positions[:, None] - grid                   # fractional distances

    # Windowed sinc: sinc(d) * Hann(d / half_width)
    w = np.sinc(d) * (0.5 + 0.5 * np.cos(np.pi * d / half_width))
    valid = (grid >= 0) & (grid <= len(x) - 1)
    gclip = np.clip(grid, 0, len(x) - 1).astype(np.int64)
    w = np.where(valid, w, 0.0)
    num = (x[gclip] * w).sum(axis=1)
    den = w.sum(axis=1)
    den = np.where(np.abs(den) < 1e-12, 1.0, den)
    return num / den


def correct_clock(
    samples: np.ndarray,
    fs: int,
    *,
    offset_s: float,
    drift_ppm: float,
    half_width: int = 16,
) -> CorrectedAudio:
    """Render the target recording onto the reference sample grid.

    Output sample n corresponds to reference time n/fs, i.e. to target
    position (offset_s + (1+drift)*n/fs) * fs. Samples whose source position
    would fall outside the valid interpolation window are dropped, so the
    output is slightly shorter than the naive duration — the exact mapping is
    returned in the CorrectedAudio metadata.
    """
    drift = drift_ppm * 1e-6
    first_pos = offset_s * fs
    last_pos = first_pos + (1.0 + drift) * (len(samples) / fs) * fs / 1.0
    # Valid interpolation positions: [half_width - 1, len - half_width]
    lo = max(first_pos, float(half_width - 1))
    hi = min(last_pos, float(len(samples) - half_width))
    if hi <= lo:
        return CorrectedAudio(
            samples=np.zeros(0), sample_rate=fs,
            first_target_position=first_pos, drift_ratio=1.0 + drift,
            half_width_taps=half_width,
        )
    n_out = int(np.floor((hi - lo) / (1.0 + drift))) + 1
    positions = lo + (1.0 + drift) * np.arange(n_out)
    out = windowed_sinc_at(samples, positions, half_width)
    return CorrectedAudio(
        samples=out, sample_rate=fs,
        first_target_position=float(lo), drift_ratio=1.0 + drift,
        half_width_taps=half_width,
    )
