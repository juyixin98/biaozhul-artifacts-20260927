"""FFT-based correlation primitives used by both pulse detection and the
content-correlation sync path.

Kept dependency-free (numpy only) and fully exercised by the unit tests, so the
two higher-level sync estimators share one verified correlation core.
"""
from __future__ import annotations

import numpy as np


def fft_xcorr(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Full cross-correlation, numerically identical to
    ``numpy.correlate(a, b, mode='full')``.

    Returns length ``len(a) + len(b) - 1``::

        out[d + (len(b) - 1)] = sum_n a[n] * b[n + d]

    for lags ``d`` from ``-(len(b)-1)`` to ``len(a)-1``.

    Sign convention: a feature present in both arrays that sits ``delta``
    samples *later* in ``b`` than in ``a`` produces a peak at lag
    ``d = -delta``.
    """
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    if a.size == 0 or b.size == 0:
        raise ValueError("fft_xcorr inputs must be non-empty")
    n = a.size + b.size - 1
    nfft = 1 << (n - 1).bit_length()
    fa = np.fft.rfft(a, nfft)
    fb = np.fft.rfft(b, nfft)
    # circ[k'] = sum_n a[n] b[n+k'] (zero-padded circular correlation).
    circ = np.fft.irfft(fa * np.conj(fb), nfft)
    # numpy.correlate(a, b, 'full') uses lag convention
    #   out[d + (len(b)-1)] = sum_n a[n] b[n+d], d = -(len(b)-1)..len(a)-1
    # Negative d wraps to the end of the circular spectrum.
    out = np.empty(n, dtype=np.float64)
    out[: b.size - 1] = circ[nfft - (b.size - 1):]
    out[b.size - 1:] = circ[: a.size]
    return out


def parabolic_interp(y_m1: float | np.ndarray, y_0: float | np.ndarray,
                     y_p1: float | np.ndarray) -> float | np.ndarray:
    """Sub-sample location of the peak of the parabola through three points.

    Returns delta in [-0.5, 0.5] relative to the centre index, positive toward
    the ``+1`` neighbour (a higher right shoulder gives a positive delta).
    """
    denom = y_m1 - 2.0 * y_0 + y_p1
    delta = 0.5 * (y_m1 - y_p1) / np.where(denom == 0, 1e-30, denom)
    return np.clip(delta, -0.5, 0.5)
