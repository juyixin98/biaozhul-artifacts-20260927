"""Sync-point detection via normalized cross-correlation with a pulse template.

The template is extracted from the *reference* recording at a declared pulse
location, then matched against the *target* recording. Detected peaks are
paired with the declared reference pulse times. A detected peak is evidence
of relative alignment only — never of absolute time.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


class NoSyncPointsError(Exception):
    """Raised when no correlation peaks pass the detection threshold."""


@dataclass(frozen=True)
class SyncPoint:
    """One measured correspondence between the two timelines."""

    ref_time_s: float        # declared pulse time on the reference clock
    measured_time_s: float   # detected pulse time on the target clock
    offset_s: float          # measured_time_s - ref_time_s
    peak_score: float        # normalized correlation at the peak, in (0, 1]


def _next_pow2(n: int) -> int:
    return 1 << (n - 1).bit_length()


def normalized_xcorr(signal: np.ndarray, template: np.ndarray) -> np.ndarray:
    """Normalized cross-correlation of `template` against `signal`.

    Returns an array of length len(signal) where entry k is the correlation
    of template with signal[k : k+len(template)], normalized to [-1, 1].
    """
    n_sig, n_tpl = len(signal), len(template)
    if n_tpl > n_sig:
        return np.zeros(0)
    n_fft = _next_pow2(n_sig + n_tpl - 1)
    spec_sig = np.fft.rfft(signal, n_fft)
    spec_tpl = np.fft.rfft(template, n_fft)
    corr = np.fft.irfft(spec_sig * np.conj(spec_tpl), n_fft)[: n_sig - n_tpl + 1]

    energy = np.concatenate(([0.0], np.cumsum(signal**2)))
    local = energy[n_tpl:] - energy[:-n_tpl]
    norm = np.sqrt(np.maximum(local, 1e-18)) * np.linalg.norm(template)
    out = np.zeros(n_sig)
    out[: len(corr)] = corr / np.maximum(norm, 1e-18)
    return out


def _parabolic_peak(y: np.ndarray, i: int) -> float:
    """Sub-sample peak position via parabolic interpolation around index i."""
    if i <= 0 or i >= len(y) - 1:
        return float(i)
    y0, y1, y2 = y[i - 1], y[i], y[i + 1]
    denom = y0 - 2.0 * y1 + y2
    if abs(denom) < 1e-12:
        return float(i)
    return float(i) + 0.5 * (y0 - y2) / denom


def find_peaks(corr: np.ndarray, fs: int, threshold: float,
               min_separation_s: float) -> list[tuple[float, float]]:
    """Greedy non-maximum-suppression peak picking over a correlation curve."""
    candidates = np.flatnonzero(corr >= threshold)
    if len(candidates) == 0:
        return []
    order = candidates[np.argsort(corr[candidates])[::-1]]
    min_sep = max(1, int(min_separation_s * fs))
    taken = np.zeros(len(corr), dtype=bool)
    peaks: list[tuple[float, float]] = []
    for idx in order:
        if taken[idx]:
            continue
        lo, hi = max(0, idx - min_sep), min(len(corr), idx + min_sep + 1)
        taken[lo:hi] = True
        pos = _parabolic_peak(corr, int(idx))
        peaks.append((pos / fs, float(corr[idx])))
    peaks.sort(key=lambda p: p[0])
    return peaks


def detect_sync_points(
    target: np.ndarray,
    fs: int,
    template: np.ndarray,
    expected_ref_times_s: list[float],
    *,
    threshold: float,
    max_pairing_offset_s: float,
    min_peak_separation_s: float,
) -> list[SyncPoint]:
    """Detect pulses in `target` and pair each declared reference pulse time
    with the nearest detected peak within `max_pairing_offset_s`.

    Pairing prefers the smallest |detected - expected| time difference: a
    spurious pulse landing closer to an expected time than the real one can
    win the pairing, producing an outlier sync point — that is deliberate,
    and it is the robust fitter's job (core.fit) to reject it downstream.
    """
    corr = normalized_xcorr(target, template)
    peaks = find_peaks(corr, fs, threshold, min_peak_separation_s)
    if not peaks:
        raise NoSyncPointsError(
            f"no correlation peaks above threshold {threshold} in target"
        )

    # Greedy assignment of expected times to detected peaks, preferring the
    # smallest |detected - expected| (the declared pulse schedule is the
    # prior). A spurious pulse landing closer to an expected time than the
    # real one wins the pairing and becomes an outlier sync point — that is
    # deliberate, and it is the robust fitter's job (core.fit) to reject it.
    pairs: list[tuple[int, int]] = []  # (expected_idx, peak_idx)
    candidates: list[tuple[float, float, int, int]] = []
    for ei, ref_t in enumerate(expected_ref_times_s):
        for pi, (peak_t, score) in enumerate(peaks):
            dt = abs(peak_t - ref_t)
            if dt <= max_pairing_offset_s:
                candidates.append((dt, -score, ei, pi))
    candidates.sort()
    used_e: set[int] = set()
    used_p: set[int] = set()
    for _dt, _neg_score, ei, pi in candidates:
        if ei in used_e or pi in used_p:
            continue
        used_e.add(ei)
        used_p.add(pi)
        pairs.append((ei, pi))

    points = [
        SyncPoint(
            ref_time_s=float(expected_ref_times_s[ei]),
            measured_time_s=float(peaks[pi][0]),
            offset_s=float(peaks[pi][0] - expected_ref_times_s[ei]),
            peak_score=float(peaks[pi][1]),
        )
        for ei, pi in sorted(pairs)
    ]
    if not points:
        raise NoSyncPointsError(
            "correlation peaks found but none could be paired with declared "
            "reference pulse times"
        )
    return points
