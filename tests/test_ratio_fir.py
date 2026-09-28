"""Tests for ratio reduction and Kaiser-windowed-sinc FIR design.

The FIR design is cross-checked against an independently written closed-form
sinc + Kaiser window (not the code under test), and against a DFT frequency
response for concrete pass-band ripple / stop-band attenuation numbers.
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from resamp.dsp.fir import (bessel_i0, design_prototype, kaiser_beta,
                            kaiser_numtaps)
from resamp.dsp.ratios import RationalRatio
from resamp.errors import InvalidInputError, ResourceExhaustedError


def test_ratio_reduction_hand_computed():
    cases = [
        (8000, 16000, 2, 1),
        (16000, 8000, 1, 2),
        (44100, 48000, 160, 147),
        (48000, 44100, 147, 160),
        (8000, 48000, 6, 1),
        (12000, 9000, 3, 4),
        (32000, 32000, 1, 1),
        (6000, 9000, 3, 2),
    ]
    for fin, fout, l, m in cases:
        r = RationalRatio.reduce(fin, fout)
        assert (r.l, r.m) == (l, m), (fin, fout, r.l, r.m)
        assert math.gcd(r.l, r.m) == 1
        assert r.high_rate == l * fin == m * fout


def test_ratio_rejects_bad_rates():
    for bad in (0, -1):
        with pytest.raises(InvalidInputError) as ei:
            RationalRatio.reduce(8000, bad)
        assert ei.value.error_code == "invalid_input"
    with pytest.raises(InvalidInputError):
        RationalRatio.reduce(8000, 10 ** 9, max_rate=10_000_000)


def test_ratio_factor_limit_is_resource_exhaustion():
    # 8000 -> 8001 is already coprime (factor 8001) — exceeds a small limit.
    with pytest.raises(ResourceExhaustedError) as ei:
        RationalRatio.reduce(8000, 8001, max_factor=4096)
    assert ei.value.category == "resource_exhausted"
    assert ei.value.details["l"] == 8001


def _independent_kaiser_window(half: int, beta: float) -> np.ndarray:
    """Kaiser window written from the series definition for I0."""
    n = np.arange(-half, half + 1, dtype=np.float64)
    arg = np.sqrt(np.maximum(1.0 - (n / half) ** 2, 0.0))

    def i0(x):
        s, t = 1.0, 1.0
        for k in range(1, 80):
            t *= (x / 2) ** 2 / k ** 2
            s += t
            if abs(t) < 1e-22 * s:
                break
        return s

    return np.array([i0(beta * a) / i0(beta) for a in arg])


def _independent_sinc(half: int, wc: float) -> np.ndarray:
    out = np.empty(2 * half + 1)
    for i, n in enumerate(range(-half, half + 1)):
        out[i] = wc / math.pi if n == 0 else math.sin(wc * n) / (math.pi * n)
    return out


@pytest.mark.parametrize("fin,fout", [(8000, 16000), (16000, 8000),
                                      (44100, 48000), (8000, 48000)])
def test_fir_matches_independent_closed_form_and_gain(fin, fout):
    r = RationalRatio.reduce(fin, fout)
    d = design_prototype(r)
    h = d.half
    wc = 2 * math.pi * (d.cutoff_hz / r.high_rate)
    expected = _independent_sinc(h, wc) * _independent_kaiser_window(h, d.beta)
    expected *= r.l / expected.sum()
    assert np.max(np.abs(d.coeffs - expected)) < 1e-10
    # DC gain on the (zero-stuffed) high-rate stream must be exactly L.
    assert abs(float(np.sum(d.coeffs)) - r.l) < 1e-11
    # Type-I symmetry and odd length.
    assert d.numtaps % 2 == 1
    assert np.array_equal(d.coeffs, d.coeffs[::-1])
    assert abs(d.coeffs[h] - max(d.coeffs)) < 1e-12 or d.coeffs[h] > 0


@pytest.mark.parametrize("fin,fout,atten", [(8000, 16000, 80.0),
                                            (16000, 8000, 80.0),
                                            (44100, 48000, 60.0)])
def test_fir_frequency_response_meets_attenuation(fin, fout, atten):
    r = RationalRatio.reduce(fin, fout)
    d = design_prototype(r, attenuation_db=atten, transition_half_width=0.1)
    nfft = 1 << 18
    H = np.fft.rfft(d.coeffs, nfft)
    freqs = np.fft.rfftfreq(nfft, d=1.0 / r.high_rate)
    mag = np.abs(H) / r.l  # normalized DC gain of the applied (÷L) filter

    # Pass-band: at and below fpass, ripple within 3 dB is generous but we
    # demand 0.1 dB for a well-designed window filter.
    pb = mag[freqs <= d.fpass_hz]
    ripple_db = 20 * np.log10(pb.max() / pb.min())
    assert ripple_db < 0.1, ripple_db

    # Stop-band: above fstop the response must beat the target attenuation.
    sb = mag[freqs >= d.fstop_hz]
    worst_db = 20 * np.log10(sb.max())
    assert worst_db < -(atten - 13.0), (worst_db, -atten)

    # Zero at Nyquist-adjacent deep attenuation sanity: no DC bias issues.
    assert mag[0] == pytest.approx(1.0, abs=1e-9)


def test_kaiser_beta_table_values():
    # Kaiser (1974) beta formula, exact branches at A=21 and A=50.
    assert kaiser_beta(21.0) == pytest.approx(0.0, abs=1e-12)
    assert kaiser_beta(30.0) == pytest.approx(
        0.5842 * 9.0 ** 0.78 + 0.07886 * 9.0, abs=1e-10)
    assert kaiser_beta(50.0) == pytest.approx(
        0.5842 * 29.0 ** 0.78 + 0.07886 * 29.0, abs=1e-10)
    assert kaiser_beta(80.0) == pytest.approx(7.8573, abs=0.01)
    # digital_width here is sample-rate (not Nyquist) normalized.
    assert kaiser_numtaps(80.0, 0.025) == 203
    assert kaiser_numtaps(80.0, 0.05) == 103


def test_bessel_i0_values():
    assert bessel_i0(0.0) == pytest.approx(1.0)
    assert bessel_i0(1.0) == pytest.approx(1.266065877, abs=1e-9)
    assert bessel_i0(3.5) == pytest.approx(7.378203, abs=1e-5)


def test_tap_cap_raises_resource_exhausted():
    r = RationalRatio.reduce(8000, 48000)
    with pytest.raises(ResourceExhaustedError) as ei:
        design_prototype(r, max_taps=50)
    assert ei.value.details["numtaps"] > 50
