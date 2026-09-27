"""Unit tests for the FFT correlation primitives.

The convention is asserted against numpy's own correlate, so a sign/index
regression in the drift estimator's foundation fails loudly instead of
silently producing a wrong offset.
"""
from __future__ import annotations

import numpy as np
import pytest

from clockalign.fftutil import fft_xcorr, parabolic_interp


def test_fft_xcorr_matches_numpy_full_correlation():
    rng = np.random.default_rng(7)
    for na, nb in [(1, 1), (5, 7), (7, 5), (3, 10), (33, 65), (129, 128)]:
        a = rng.standard_normal(na)
        b = rng.standard_normal(nb)
        np.testing.assert_allclose(
            fft_xcorr(a, b), np.correlate(a, b, "full"), atol=1e-10)


def test_peak_lag_sign_and_position():
    # correlate(a, b) lag d: sum a[n-d] b[n]. A feature that sits 3 samples
    # LATER in b gives lag d = -3. Callers map this sign explicitly.
    a = np.zeros(20)
    a[5:9] = [1, 2, 3, 2]
    b = np.zeros(20)
    b[8:12] = [1, 2, 3, 2]
    c = fft_xcorr(a, b)
    lags = np.arange(-(b.size - 1), a.size)
    assert lags[int(np.argmax(c))] == -3


def test_parabolic_interp_zero_at_symmetric_peak():
    assert abs(float(parabolic_interp(1.0, 2.0, 1.0))) < 1e-12


def test_parabolic_interp_direction():
    # Closed-form value; a higher LEFT shoulder biases the peak to the left.
    y_m1, y0, y_p1 = 1.5, 2.0, 1.0
    expected = 0.5 * (y_m1 - y_p1) / (y_m1 - 2 * y0 + y_p1)
    d = float(parabolic_interp(y_m1, y0, y_p1))
    assert d == pytest.approx(expected)
    assert d < 0  # left-shoulder higher -> peak offset to the left
    assert abs(float(parabolic_interp(1.0, 2.0, 1.0))) < 1e-12
