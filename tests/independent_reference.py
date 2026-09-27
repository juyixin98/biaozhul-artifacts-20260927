"""Independent EBU R128 reference used ONLY by the test suite.

This is deliberately written from scratch rather than importing the
production kernel: a different blocking routine (strided windows over the
fully filtered signal), a different filter call path (scipy sos-style direct
lfilter on the complete signal), and energy-accumulation gating. It is used to
cross-check the streaming implementation. pyloudnorm and the ffmpeg CLI add
two more, genuinely external, references in test_references.py.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.signal import lfilter

LOUDNESS_OFFSET = -0.691
WEIGHTS_5 = np.array([1.0, 1.0, 1.0, 1.41, 1.41])


# Coefficients are re-derived here from the RBJ Audio EQ Cookbook formulas
# (not imported from app.kernel) so this reference stays an independent code path.
def _shelf(sample_rate: int):
    gain_db, q, fc = 4.0, 1.0 / np.sqrt(2.0), 1500.0
    A = 10.0 ** (gain_db / 40.0)
    w0 = 2 * np.pi * fc / sample_rate
    alpha = np.sin(w0) / (2 * q)
    c = np.cos(w0)
    sA = np.sqrt(A)
    b = np.array([A * ((A + 1) + (A - 1) * c + 2 * sA * alpha),
                  -2 * A * ((A - 1) + (A + 1) * c),
                  A * ((A + 1) + (A - 1) * c - 2 * sA * alpha)])
    a = np.array([(A + 1) - (A - 1) * c + 2 * sA * alpha,
                  2 * ((A - 1) - (A + 1) * c),
                  (A + 1) - (A - 1) * c - 2 * sA * alpha])
    return b / a[0], a / a[0]


def _highpass(sample_rate: int):
    q, fc = 0.5, 38.0
    w0 = 2 * np.pi * fc / sample_rate
    alpha = np.sin(w0) / (2 * q)
    c = np.cos(w0)
    b = np.array([(1 + c) / 2, -(1 + c), (1 + c) / 2])
    a = np.array([1 + alpha, -2 * c, 1 - alpha])
    return b / a[0], a / a[0]


@dataclass
class RefResult:
    integrated_lufs: float | None
    lra_lu: float | None
    integrated_status: str
    lra_status: str
    m_block_loudness: list[float]
    s_block_loudness: list[float]
    m_total: int
    m_above_abs: int
    m_above_both: int
    m_relative_gate: float | None
    s_total: int
    s_above_abs: int
    s_above_both: int
    s_relative_gate: float | None


def _window_ms(y: np.ndarray, block: int, hop: int, weights: np.ndarray) -> np.ndarray:
    """Mean-square energy per window per channel, computed directly per block
    (cumsum subtraction suffers catastrophic cancellation for long signals)."""
    n_blocks = 1 + max(0, (len(y) - block) // hop)
    if n_blocks <= 0:
        return np.empty((0, y.shape[1]))
    out = np.empty((n_blocks, y.shape[1]))
    for j in range(n_blocks):
        seg = y[j * hop:j * hop + block]
        out[j] = np.sum(seg * seg, axis=0) / block
    return out


def _loudness(ms: np.ndarray, weights: np.ndarray) -> np.ndarray:
    power = ms @ weights
    with np.errstate(divide="ignore"):
        loud = np.where(power > 0, LOUDNESS_OFFSET + 10 * np.log10(power), -np.inf)
    return loud


def reference_measure(samples: np.ndarray, sample_rate: int,
                      n_analysis_channels: int) -> RefResult:
    if samples.ndim == 1:
        samples = samples[:, None]
    weights = WEIGHTS_5[:n_analysis_channels]

    # Filter whole signal in one shot (independent of streaming state code).
    b_s, a_s = _shelf(sample_rate)
    b_h, a_h = _highpass(sample_rate)
    y = lfilter(b_h, a_h, lfilter(b_s, a_s, samples, axis=0), axis=0)

    m_block = int(round(0.400 * sample_rate))
    s_block = int(round(3.000 * sample_rate))
    hop = int(round(0.100 * sample_rate))

    m_ms = _window_ms(y, m_block, hop, weights)
    s_ms = _window_ms(y, s_block, hop, weights)
    m_loud = _loudness(m_ms, weights)
    s_loud = _loudness(s_ms, weights)

    # --- integrated gating (Tech 3341) ---
    if len(m_loud) == 0:
        i_status, integrated, m_rel, m_both = "INSUFFICIENT_BLOCKS", None, None, 0
        m_abs = 0
    else:
        abs_mask = m_loud >= -70.0
        m_abs = int(abs_mask.sum())
        if m_abs == 0:
            i_status, integrated, m_rel, m_both = "SILENCE", None, None, 0
        else:
            ungated_power = float(np.sum(weights * m_ms[abs_mask].mean(axis=0)))
            ungated = LOUDNESS_OFFSET + 10 * np.log10(ungated_power)
            m_rel = ungated - 10.0
            both = abs_mask & (m_loud > m_rel)
            m_both = int(both.sum())
            sel_power = float(np.sum(weights * m_ms[both].mean(axis=0)))
            integrated = LOUDNESS_OFFSET + 10 * np.log10(sel_power)
            i_status = "OK"

    # --- LRA gating (Tech 3342) ---
    if len(s_loud) == 0:
        l_status, lra, s_rel, s_both = (
            i_status if i_status == "SILENCE" else "INSUFFICIENT_BLOCKS",
            None, None, 0)
        s_abs = 0
    else:
        abs_mask = s_loud >= -70.0
        s_abs = int(abs_mask.sum())
        if s_abs == 0:
            l_status, lra, s_rel, s_both = "SILENCE", None, None, 0
        else:
            st_power = float(np.sum(weights * s_ms[abs_mask].mean(axis=0)))
            s_rel = LOUDNESS_OFFSET + 10 * np.log10(st_power) - 20.0
            both = abs_mask & (s_loud > s_rel)
            s_both = int(both.sum())
            if s_both == 0:
                l_status, lra = "SILENCE", None
            else:
                selected = np.sort(s_loud[both])
                lra = float(np.percentile(selected, 95) - np.percentile(selected, 10))
                l_status = "OK"

    return RefResult(
        integrated_lufs=integrated, lra_lu=lra,
        integrated_status=i_status, lra_status=l_status,
        m_block_loudness=[None if np.isneginf(v) else float(v) for v in m_loud],
        s_block_loudness=[None if np.isneginf(v) else float(v) for v in s_loud],
        m_total=len(m_loud), m_above_abs=m_abs, m_above_both=m_both,
        m_relative_gate=m_rel,
        s_total=len(s_loud), s_above_abs=s_abs, s_above_both=s_both,
        s_relative_gate=s_rel)
