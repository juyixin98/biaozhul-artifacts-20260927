"""Short streams and chunking invariance (the reviewer's two main lenses)."""
from __future__ import annotations

import numpy as np
import pytest

from resamp.dsp.fir import design_prototype
from resamp.dsp.polyphase import PolyphaseResampler
from resamp.dsp.ratios import RationalRatio
from resamp.dsp.reference import resample_offline


RATIOS = [(8000, 16000), (16000, 8000), (44100, 48000),
          (48000, 44100), (8000, 48000), (12000, 9000), (32000, 32000)]


def run_chunked(fin, fout, x, sizes):
    rs = PolyphaseResampler(fin, fout)
    outs, i = [], 0
    for s in sizes:
        outs.append(rs.push(x[i:i + s]))
        i += s
    assert i == x.size
    outs.append(rs.flush())
    return np.concatenate(outs)


@pytest.mark.parametrize("fin,fout", RATIOS)
@pytest.mark.parametrize("n", [0, 1, 2, 3, 4, 8, 31])
def test_exact_counts_for_short_streams(fin, fout, n):
    r = RationalRatio.reduce(fin, fout)
    d = design_prototype(r)
    x = np.arange(1, n + 1, dtype=np.float64) * 0.1
    rs = PolyphaseResampler(fin, fout)
    out = np.concatenate([rs.push(x), rs.flush()])
    expected = PolyphaseResampler.expected_output_count(n, r, d)
    assert out.size == expected
    assert rs.total_output_samples == expected
    if n:
        ref = resample_offline(x, fin, fout)[0]
        assert np.max(np.abs(out - ref)) < 1e-10


def test_empty_then_flush_is_empty():
    rs = PolyphaseResampler(8000, 16000)
    assert rs.push(np.empty(0, dtype=np.float64)).size == 0
    tail = rs.flush()
    assert tail.size == 0
    assert rs.total_output_samples == 0


def test_flush_only_without_any_push():
    rs = PolyphaseResampler(44100, 48000)
    assert rs.flush().size == 0


@pytest.mark.parametrize("fin,fout", RATIOS)
def test_many_chunkings_bit_identical(fin, fout, log):
    rng = np.random.default_rng(7)
    n = 1200
    x = rng.standard_normal(n)
    x += 0.5 * np.sin(2 * np.pi * 333 * np.arange(n) / fin)
    whole = run_chunked(fin, fout, x, [n])

    plans = [
        [1] * 200 + [n - 200],
        [2] * 100 + [n - 200],
        ([3, 1, 4, 1, 5] * (n // 14)
         + [n - 14 * (n // 14)]),
        [100] * (n // 100) + [n - 100 * (n // 100)],
        [512, n - 512],
        [7, 7, 7, n - 21],
    ]
    for pi, sizes in enumerate(plans):
        sizes = [s for s in sizes if s > 0]
        assert sum(sizes) == n, (pi, sum(sizes))
        y = run_chunked(fin, fout, x, sizes)
        assert y.shape == whole.shape
        assert np.array_equal(y, whole), (fin, fout, pi,
                                          np.max(np.abs(y - whole)))
    ref, _ = resample_offline(x, fin, fout)
    log("chunking", ok=True,
        reason=f"{len(plans)} chunking plans bit-identical and match FFT ref",
        plans=len(plans), n_out=int(whole.size),
        ref_max_err=float(np.max(np.abs(whole - ref))))
    assert np.max(np.abs(whole - ref)) < 1e-9


def test_empty_chunks_interspersed_do_not_change_result():
    fin, fout = 8000, 48000
    x = np.random.default_rng(3).standard_normal(2000)
    rs = PolyphaseResampler(fin, fout)
    parts = [rs.push(x[:500]), rs.push(np.empty(0)), rs.push(x[500:1500]),
             rs.push(np.empty(0)), rs.push(x[1500:]), rs.flush()]
    y = np.concatenate(parts)
    whole = run_chunked(fin, fout, x, [x.size])
    assert np.array_equal(y, whole)


def test_streaming_prefix_is_bitwise_prefix_of_full_output():
    """Outputs already emitted before flush must equal the same n indices of
    the single-shot result — the core must not retroactively change earlier
    samples (guards against alignment-dependent BLAS reduction)."""
    rng = np.random.default_rng(31)
    for fin, fout in [(8000, 48000), (44100, 48000), (16000, 8000)]:
        x = rng.standard_normal(10000)
        r = PolyphaseResampler(fin, fout)
        parts = [r.push(x[:3000]), r.push(x[3000:7000]),
                 r.push(x[7000:])]
        streamed = np.concatenate(parts)
        bulk = PolyphaseResampler(fin, fout)
        full = np.concatenate([bulk.push(x), bulk.flush()])
        assert np.array_equal(streamed, full[:streamed.size]), (fin, fout)
        tail = r.flush()
        assert np.array_equal(np.concatenate([streamed, tail]), full)


def test_counters_advance_only_with_real_output():
    rs = PolyphaseResampler(16000, 8000)
    a = rs.push(np.zeros(1))
    # First input alone cannot produce a streaming output (taps need history).
    assert rs.total_input_samples == 1
    assert rs.total_output_samples == a.size
    b = rs.flush()
    assert rs.total_output_samples == a.size + b.size
