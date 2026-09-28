"""Kernel-level tests: impulse response, polyphase arms, counts, phasing."""
from __future__ import annotations

import numpy as np
import pytest

from resamp.dsp.fir import design_prototype
from resamp.dsp.polyphase import PolyphaseResampler
from resamp.dsp.ratios import RationalRatio
from resamp.errors import InvalidInputError


RATIOS = [(8000, 16000), (16000, 8000), (44100, 48000),
          (48000, 44100), (8000, 48000), (9000, 12000), (1, 1)]


def _stream(fin, fout, x, chunks):
    rs = PolyphaseResampler(fin, fout)
    parts = []
    for c in chunks(x):
        parts.append(rs.push(c))
    parts.append(rs.flush())
    return np.concatenate(parts)


@pytest.mark.parametrize("fin,fout", RATIOS)
def test_impulse_response_matches_prototype_phases(fin, fout):
    r = RationalRatio.reduce(fin, fout)
    d = design_prototype(r)
    l, m, h = r.l, r.m, d.half
    # Long enough that the impulse response settles inside the stream.
    n_in = 4 * h + 50
    x = np.zeros(n_in)
    x[2 * h // l + 5] = 1.0
    rs = PolyphaseResampler(fin, fout)
    y = np.concatenate([rs.push(x), rs.flush()])

    # Direct model: zero-stuff, full convolution with the prototype.
    xz = np.zeros(l * n_in)
    xz[::l] = x
    conv = np.convolve(xz, d.coeffs)
    n_out = PolyphaseResampler.expected_output_count(n_in, r, d)
    n = np.arange(n_out)
    expected = conv[n * m + h]
    assert y.shape == expected.shape
    # Impulse path is short and exact.
    assert np.max(np.abs(y - expected)) < 1e-12


@pytest.mark.parametrize("fin,fout", RATIOS)
def test_polyphase_arms_partition_the_prototype(fin, fout):
    """The arms partition every prototype tap; therefore the sum of all arm
    coefficients is exactly L (DC gain on the zero-stuffed stream).  Individual
    arm sums need not equal 1 — their DC gain depends on the zero-stuffing
    phase — only the partition identity and the total are invariant."""
    r = RationalRatio.reduce(fin, fout)
    d = design_prototype(r)
    l, h = r.l, d.half
    covered = np.zeros_like(d.coeffs, dtype=bool)
    total = 0.0
    for p in range(l):
        j = np.arange(-((h + p) // l), (h - p) // l + 1)
        idx = h + p + j * l
        assert np.all(idx >= 0) and np.all(idx < d.numtaps)
        assert not np.any(covered[idx]), (fin, fout, p)
        covered[idx] = True
        total += float(d.coeffs[idx].sum())
    assert covered.all()
    assert total == pytest.approx(float(l), abs=1e-11)


@pytest.mark.parametrize("fin,fout", RATIOS)
def test_sample_count_formula(fin, fout):
    r = RationalRatio.reduce(fin, fout)
    rs = PolyphaseResampler(fin, fout)
    d = rs.design
    for n_in in [0, 1, 2, 3, 5, 7, 100, 1001]:
        x = np.zeros(n_in)
        rs.reset()
        out = np.concatenate([rs.push(x), rs.flush()])
        expected = PolyphaseResampler.expected_output_count(n_in, r, d)
        assert out.size == expected, (fin, fout, n_in, out.size, expected)
        rs.reset()


@pytest.mark.parametrize("fin,fout", [(8000, 16000), (16000, 8000),
                                      (44100, 48000)])
def test_filter_phase_state_carries_across_chunks(fin, fout):
    """A tone whose relevant taps straddle a chunk boundary must be identical
    to the single-chunk result.  Boundaries are swept densely around the
    filter support and sampled sparsely elsewhere."""
    r = RationalRatio.reduce(fin, fout)
    d = design_prototype(r)
    n_in = 2 * d.half + 400
    t = np.arange(n_in) / fin
    x = 0.7 * np.sin(2 * np.pi * 0.12 * fin * t)
    whole = _stream(fin, fout, x, lambda z: [z])
    support = d.half // r.l + 3
    boundaries = (set(range(1, 2 * support + 10))
                  | set(range(2 * support, n_in, 97)))
    for boundary in sorted(boundaries):
        rs = PolyphaseResampler(fin, fout)
        y = np.concatenate([rs.push(x[:boundary]),
                            rs.push(x[boundary:]), rs.flush()])
        assert np.array_equal(y, whole), (fin, fout, boundary)


def test_group_delay_and_padding_reported_consistently():
    rs = PolyphaseResampler(8000, 16000)
    s = rs.design_summary()
    h = rs.design.half
    l, m = rs.ratio.l, rs.ratio.m
    assert s["group_delay_input_samples"] == h / l
    assert s["group_delay_output_samples"] == h / m
    assert s["group_delay_seconds"] == h / rs.ratio.high_rate
    assert s["head_pad_input_samples"] == h // l
    assert s["tail_pad_input_samples"] == 2 * h // l + 2
    # Impulse well inside the stream: output peak lands at floor(L*p/M), the
    # boundary-independent position implied by the fixed head pad (the
    # symmetric filter's H-tap delay is absorbed by that padding).
    p = 200
    n_in = 600
    x = np.zeros(n_in)
    x[p] = 1.0
    y = np.concatenate([rs.push(x), rs.flush()])
    peak = int(np.argmax(np.abs(y)))
    assert peak == (l * p) // m
    assert abs(y[peak] - 1.000006466660007) < 1e-9


def test_1_to_1_midstream_impulse_peaks_at_same_index():
    rs = PolyphaseResampler(1000, 1000)
    h = rs.design.half
    x = np.zeros(60)
    x[20] = 1.0
    y = np.concatenate([rs.push(x), rs.flush()])
    # With the fixed head pad of H zeros, the symmetric FIR's delay is absorbed
    # by the padding: the impulse peaks at the *same* sample index n=20 (its
    # center tap value), both for 1:1 and (by floor(L*p/M)) generally.
    peak = int(np.argmax(np.abs(y)))
    assert peak == 20
    assert y[peak] == pytest.approx(rs.design.coeffs[h])
    # Outputs after the (padded) impulse response has ended are exactly zero.
    assert np.all(y[20 + h + 1:] == 0.0)


def test_nonfinite_input_index_reported():
    rs = PolyphaseResampler(8000, 16000)
    x = np.zeros(10)
    x[4] = np.inf
    with pytest.raises(InvalidInputError) as ei:
        rs.push(x)
    assert ei.value.details["index"] == 4


def test_reset_restores_initial_state():
    rs = PolyphaseResampler(8000, 16000)
    rs.push(np.ones(100))
    rs.reset()
    assert rs.total_input_samples == 0
    assert rs.total_output_samples == 0
    assert not rs.flushed
    x = np.zeros(5)
    x[0] = 1.0
    a = np.concatenate([rs.push(x), rs.flush()])
    rs.reset()
    b = np.concatenate([rs.push(x), rs.flush()])
    assert np.array_equal(a, b)
