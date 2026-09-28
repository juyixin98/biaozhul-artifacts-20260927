"""Impulse response and exact agreement with independent references."""

from __future__ import annotations

import numpy as np
import pytest

from reference import SCIPY_AVAILABLE, fft_poly_ref, scipy_poly_ref
from resampler.signal import StreamingPolyphase, build_plan

RATIOS = [(16000, 48000), (48000, 16000), (48000, 44100),
          (44100, 48000), (11025, 48000), (22050, 8000), (8000, 8000)]


def stream_all(plan, x, chunk):
    eng = StreamingPolyphase(plan)
    outs = []
    for i in range(0, x.size, chunk):
        outs.append(eng.push(x[i:i + chunk]))
    outs.append(eng.flush())
    return np.concatenate(outs) if outs else np.empty(0)


@pytest.mark.parametrize("rate_in,rate_out", RATIOS)
def test_exact_match_scipy_upfirdn(rate_in, rate_out, settings, runlog):
    """Same prototype, independent implementation: must agree to ~eps."""
    if not SCIPY_AVAILABLE:
        pytest.skip("scipy not installed; FFT reference covers this case")
    rng = np.random.default_rng(7)
    x = rng.standard_normal(257)
    plan = build_plan(rate_in, rate_out, settings=settings)
    y = stream_all(plan, x, chunk=37)
    ref = scipy_poly_ref(x, plan.prototype, plan.up, plan.down)
    runlog.check("length equals scipy upfirdn", y.size == ref.size,
                 {"ours": y.size, "scipy": ref.size},
                 "full-conv length identity: ceil(L*(J+K-1)/M)")
    err = float(np.max(np.abs(y - ref)))
    runlog.check("sample-wise agreement <= 4e-14 absolute", err <= 4e-14,
                 {"max_abs_err": err, "ratio": f"{rate_in}->{rate_out}"},
                 "differences are only float64 summation-order noise")


@pytest.mark.parametrize("rate_in,rate_out", RATIOS)
def test_match_independent_fft_reference(rate_in, rate_out, settings, runlog):
    """Independent filter-construction path: zero-stuff + FFT full convolution."""
    rng = np.random.default_rng(11)
    x = rng.standard_normal(200)
    plan = build_plan(rate_in, rate_out, settings=settings)
    y = stream_all(plan, x, chunk=1)
    ref = fft_poly_ref(x, plan.prototype, plan.up, plan.down)
    n = min(y.size, ref.size)
    runlog.check("FFT reference length parity", y.size == ref.size,
                 {"ours": y.size, "fft_ref": ref.size},
                 "zero-stuff/conv/decimate produces identical count")
    err = float(np.max(np.abs(y[:n] - ref[:n])))
    runlog.check("FFT reference agreement <= 1e-10", err <= 1e-10,
                 {"max_abs_err": err, "ratio": f"{rate_in}->{rate_out}"},
                 "FFT round-trip tolerance (large nfft accumulation)")


@pytest.mark.parametrize("rate_in,rate_out", RATIOS)
def test_impulse_response_is_prototype(rate_in, rate_out, settings, runlog):
    """Response to a unit impulse at x[0] equals prototype samples at n*M mod L."""
    J = 64
    x = np.zeros(J)
    x[0] = 1.0
    plan = build_plan(rate_in, rate_out, settings=settings)
    y = stream_all(plan, x, chunk=5)

    L, M, K = plan.up, plan.down, plan.taps_per_phase
    n = np.arange(y.size)
    # With the impulse at global 0 and head-padding zeros, y[n] = P[p_n, q_n]
    # = h[(K-1-q_n)*L + p_n] for q_n >= 0; only q in [0, K-1] can be nonzero.
    q = (n * M) // L
    p = (n * M) % L
    valid = (q >= 0) & (q <= K - 1)
    expected = np.zeros_like(y)
    # h index for P[p, q]: prototype.reshape(K,L).T => P[p,q]=h[q*L+p]
    expected[valid] = plan.prototype[q[valid] * L + p[valid]]
    err = float(np.max(np.abs(y - expected)))
    runlog.check("impulse response equals polyphase column taps",
                 err <= 1e-14, {"max_abs_err": err},
                 "y[n] on impulse = P[(nM)%L, floor(nM/L)]")

    # The sum of the impulse response approaches L/M (DC gain).  It is only
    # exact in the infinite-length limit; a finite window leaves a truncation
    # residual (Kaiser tail beyond K-1), bounded here by the stopband ripple.
    total = float(np.sum(y))
    runlog.check("impulse response sum ~= L/M within 5e-5 truncation residual",
                 abs(total - L / M) <= 5e-5,
                 {"sum": total, "expected": L / M},
                 "window truncation; exact equality only for infinite h")


def test_impulse_peak_delay(settings, runlog):
    """The impulse response peak sits at the documented group delay."""
    plan = build_plan(48000, 16000, settings=settings)  # L=1, M=3
    x = np.zeros(200); x[0] = 1.0
    y = stream_all(plan, x, chunk=10)
    peak = int(np.argmax(np.abs(y)))
    # t_out(n_peak) should be at ~0 input time: n*M - (K-1)/2 == 0
    n = peak
    center = n * plan.down - (plan.taps_per_phase - 1) / 2.0
    runlog.check("impulse peak at output index n with n*M == (K-1)/2",
                 abs(center) <= plan.down, {"n": n, "center": center},
                 "L=1: nearest output sample to the impulse input time")
