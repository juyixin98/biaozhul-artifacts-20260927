"""Content-based sync points via normalized cross-correlation (NCC).

Used when the recording carries no known sync pulses but the two devices did
capture the same acoustic content. Short windows of the reference track are
matched against the slave track inside a bounded lag window.

Important epistemic boundary (also surfaced in the report): a correlation peak
proves that the *same waveform* appears at two track-relative positions. It is
not, by itself, proof of an absolute wall-clock time; absolute time requires an
external anchor this backend does not invent.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .fftutil import fft_xcorr, parabolic_interp


@dataclass(frozen=True)
class CorrelationPoint:
    t_a: float
    t_b: float
    score: float


def _ncc_lags(a_win: np.ndarray, b: np.ndarray, min_lag: int,
              max_lag: int) -> np.ndarray:
    """Normalized correlation for ``correlate(b, a_win)`` lags.

    For numpy lag ``d``, the value is sum over n of b[n] * a_win[n-d]. The
    window a_win[0:w] then matches b[d:d+w], so ``d`` is the slave index where
    the window content starts. Both signals are normalized by the energies of
    the overlapping samples, which keeps partial/edge overlaps from scoring
    artificially.
    """
    w = a_win.size
    full = fft_xcorr(b, a_win)  # length len(b)+w-1; lag d at index (w-1)+d
    b_energy_cum = np.concatenate(([0.0], np.cumsum(b * b)))
    out = np.empty(max_lag - min_lag + 1, dtype=np.float64)
    for idx, d in enumerate(range(min_lag, max_lag + 1)):
        raw = full[(w - 1) + d]
        lo = max(0, d)
        hi = min(b.size, d + w)
        nlo = lo - d
        nhi = nlo + (hi - lo)
        e_b = float(b_energy_cum[hi] - b_energy_cum[lo])
        e_a = float(np.sum(a_win[nlo:nhi] ** 2))
        out[idx] = raw / np.sqrt(max(e_a * e_b, 1e-20))
    return out


def find_correlation_points(reference: np.ndarray, slave: np.ndarray,
                            sample_rate: int, *, window_s: float, hop_s: float,
                            search_half_window_s: float, min_score: float,
                            ) -> list[CorrelationPoint]:
    """Locate shared content at regular reference-time hops.

    For every reference window a[start:start+w] we search the slave array for
    the best matching start index d in ``start ± search_half_window`` -- a
    global free-lag search would happily align unrelated similar sounds far
    outside the plausible clock offset.
    """
    a = np.asarray(reference, dtype=np.float64)
    b = np.asarray(slave, dtype=np.float64)
    w = int(round(window_s * sample_rate))
    hop = int(round(hop_s * sample_rate))
    max_lag = int(round(search_half_window_s * sample_rate))
    if a.size <= w or w < 8:
        return []

    points: list[CorrelationPoint] = []
    for start in range(0, a.size - w + 1, hop):
        a_win = a[start: start + w]
        if float(np.sum(a_win * a_win)) < 1e-10:
            continue
        min_d = max(0, start - max_lag)
        max_d = min(b.size - w, start + max_lag)
        if max_d <= min_d:
            continue
        ncc = _ncc_lags(a_win, b, min_d, max_d)
        peak = int(np.argmax(ncc))
        score = float(ncc[peak])
        if score < min_score:
            continue
        if 1 <= peak < ncc.size - 1:
            delta = float(parabolic_interp(float(ncc[peak - 1]), score,
                                           float(ncc[peak + 1])))
        else:
            delta = 0.0
        d_peak = min_d + peak + delta
        # The window a[start:...] plays at slave start index d:
        # t_b = (d + w/2)/fs, t_a = (start + w/2)/fs.
        t_a = (start + w / 2.0) / sample_rate
        t_b = (d_peak + w / 2.0) / sample_rate
        points.append(CorrelationPoint(t_a=t_a, t_b=t_b, score=score))
    return points
