"""K-weighting filter tests against an independent design."""
from __future__ import annotations

import numpy as np

from app.filters import StreamingKWeighting
from app.r128_constants import (
    PRE_FILTER_A,
    PRE_FILTER_B,
    RLB_A,
    RLB_B,
)


def test_canonical_coeffs_match_pyloudnorm_deman_design(pyln_ref):
    """The hard-coded 48k constants must match the independent DeMan design."""
    import pyloudnorm
    meter = pyloudnorm.Meter(48000, filter_class="DeMan")
    shelf = next(v for k, v in meter._filters.items() if "shelf" in k)
    highpass = next(v for k, v in meter._filters.items() if "pass" in k)
    np.testing.assert_allclose(shelf.b, PRE_FILTER_B, atol=2e-7)
    np.testing.assert_allclose(shelf.a, PRE_FILTER_A, atol=2e-7)
    np.testing.assert_allclose(highpass.b, RLB_B, atol=2e-7)
    np.testing.assert_allclose(highpass.a, RLB_A, atol=2e-7)


def _response_db(b, a, w):
    z = np.exp(1j * w)
    return 20 * np.log10(abs(np.polyval(b, z) / np.polyval(a, z)))


def test_frequency_response_matches_b_spectrum():
    # DC unity, Nyquist +4 dB for the shelf; RLB kills DC.
    assert abs(_response_db(np.array(PRE_FILTER_B), np.array(PRE_FILTER_A),
                            0.0)) < 1e-9
    assert abs(_response_db(np.array(PRE_FILTER_B), np.array(PRE_FILTER_A),
                            np.pi) - 3.99984385397) < 1e-6
    assert _response_db(np.array(RLB_B), np.array(RLB_A), 1e-6) < -100


def test_streaming_filter_matches_offline_lfilter():
    from scipy.signal import lfilter
    rng = np.random.default_rng(3)
    x = rng.standard_normal((48000, 2))
    # whole signal offline
    ref = x.copy()
    for c in range(2):
        ref[:, c] = lfilter(PRE_FILTER_B, PRE_FILTER_A, ref[:, c])
        ref[:, c] = lfilter(RLB_B, RLB_A, ref[:, c])
    # streaming in odd chunks
    f = StreamingKWeighting(2)
    out = np.concatenate([f.process(chunk) for chunk in
                          [x[:9973], x[9973:20000], x[20000:41234],
                           x[41234:]]], axis=0)
    np.testing.assert_allclose(out, ref, atol=1e-12)


def test_streaming_filter_initial_transient_is_continuous():
    """Feeding silence then signal must equal feeding it all at once."""
    rng = np.random.default_rng(4)
    sig = rng.standard_normal((10000, 1))
    whole = np.concatenate([np.zeros((2000, 1)), sig], axis=0)
    a = StreamingKWeighting(1).process(whole)
    f = StreamingKWeighting(1)
    b = np.concatenate([f.process(np.zeros((2000, 1))), f.process(sig)], axis=0)
    np.testing.assert_allclose(a, b, atol=1e-12)
