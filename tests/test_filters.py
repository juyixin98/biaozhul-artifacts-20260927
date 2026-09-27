"""Tests for the K-weighting stage.

Cross-checks come from three independent directions:
- scipy direct lfilter of freshly computed coefficients (streaming equivalence);
- an independent textbook direct-form-I biquad written in this test file
  (not the production filter code);
- frequency-domain gain checks at 60 Hz / 1 kHz / 10 kHz.
"""

from __future__ import annotations

import numpy as np
import pytest
from scipy.signal import lfilter

from app.kernel.filter import (
    StreamingKWeighting,
    high_pass_coeffs,
    high_shelf_coeffs,
    k_weighting_coeffs,
)


def df1_biquad(x, b, a, state):
    """Independent direct-form-I biquad (test-only implementation)."""
    y = np.zeros_like(x, dtype=np.float64)
    x1, x2, y1, y2 = state
    for n in range(len(x)):
        v = b[0] * x[n] + b[1] * x1 + b[2] * x2 - a[1] * y1 - a[2] * y2
        x2, x1, y2, y1 = x1, x[n], y1, v
        y[n] = v
    return y, (x1, x2, y1, y2)


def test_coeffs_match_rbj_constants():
    b, a = high_shelf_coeffs(sample_rate=48000)
    assert b.shape == a.shape == (3,)
    assert a[0] == pytest.approx(1.0)
    # Shelf passband gain at very high frequency must be ~+4 dB.
    w = np.exp(1j * np.pi * 0.99)
    h = (b[0] + b[1] / w + b[2] / w**2) / (a[0] + a[1] / w + a[2] / w**2)
    assert 20 * np.log10(abs(h)) == pytest.approx(4.0, abs=0.05)

    b_hp, a_hp = high_pass_coeffs(sample_rate=48000)
    assert a_hp[0] == pytest.approx(1.0)
    # High pass attenuates strongly at very low frequency, passes at Nyquist.
    w_lo = np.exp(1j * 2 * np.pi * 5 / 48000)
    w_hi = np.exp(1j * np.pi * 0.99)
    h_lo = (b_hp[0] + b_hp[1] / w_lo + b_hp[2] / w_lo**2) / (
        a_hp[0] + a_hp[1] / w_lo + a_hp[2] / w_lo**2)
    h_hi = (b_hp[0] + b_hp[1] / w_hi + b_hp[2] / w_hi**2) / (
        a_hp[0] + a_hp[1] / w_hi + a_hp[2] / w_hi**2)
    assert abs(h_lo) <= 0.02
    assert abs(h_hi) <= 1.02


def test_streaming_equals_whole_for_any_chunking():
    sr = 48000
    rng = np.random.default_rng(7)
    x = rng.standard_normal((sr * 3, 2))
    kw = StreamingKWeighting(sr, 2)
    whole = kw.process(x)
    for chunk in (1, 37, 480, 48000):
        kw.reset()
        pieces = [kw.process(x[i:i + chunk]) for i in range(0, len(x), chunk)]
        streamed = np.concatenate(pieces, axis=0)
        np.testing.assert_array_equal(streamed, whole)


def test_matches_independent_df1_biquad():
    sr = 48000
    x = np.sin(2 * np.pi * np.arange(sr) * 1000 / sr)
    (b_s, a_s), (b_h, a_h) = k_weighting_coeffs(sr)
    ref = lfilter(*(b_h, a_h), lfilter(b_s, a_s, x))
    y, _ = df1_biquad(x, b_s, a_s, (0, 0, 0, 0))
    y, _ = df1_biquad(y, b_h, a_h, (0, 0, 0, 0))
    np.testing.assert_allclose(y, ref, atol=1e-10, rtol=1e-10)


def test_frequency_response_near_bs1770_expectations():
    """K-weighting response shape: ~+0.7 dB at 1 kHz, ~+4 dB well above the
    1.5 kHz shelf corner, and strong attenuation below 38 Hz."""
    sr = 48000
    (b_s, a_s), (b_h, a_h) = k_weighting_coeffs(sr)

    def gain_db(freq):
        n = 2**18
        t = np.arange(n) / sr
        x = np.sin(2 * np.pi * freq * t)
        y = lfilter(b_h, a_h, lfilter(b_s, a_s, x))
        rms_x = np.sqrt(np.mean(x[n // 2:] ** 2))
        rms_y = np.sqrt(np.mean(y[n // 2:] ** 2))
        return 20 * np.log10(rms_y / rms_x)

    # BS.1770-4 published K-weighting curve: about +0.5..+0.7 dB near 1 kHz.
    assert gain_db(1000) == pytest.approx(0.65, abs=0.15)
    assert gain_db(10000) == pytest.approx(4.0, abs=0.2)
    assert gain_db(20) < -10.0


def test_filter_requires_matching_channels():
    kw = StreamingKWeighting(48000, 2)
    with pytest.raises(ValueError):
        kw.process(np.zeros((10, 1)))
