"""Independent high-precision off-line reference resampler.

This module deliberately does **not** use :mod:`resamp.dsp.polyphase`.  It
realizes the same rate conversion the textbook way:

1. explicitly zero-stuff the input by ``L`` (``xz[k] = x[k/L] if k%L==0``);
2. convolve with the prototype FIR using circular FFT convolution sized
   ``next_fast_len(len(xz) + len(h) - 1)`` (zero padding, no overlap-add);
3. sample the *full* convolution at indices ``n*M``.

The output index convention is identical to the streaming core, including the
head/tail zero padding and the deterministic count, so the two can be compared
sample-for-sample.  Only the FIR coefficients are shared (a design-table
equivalence); the arithmetic path — zero-stuff + FFT + direct indexing vs.
polyphase dot products in the time domain — is independent, which is what
makes the cross-check meaningful.
"""
from __future__ import annotations

import numpy as np

from .fir import FilterDesign
from .polyphase import PolyphaseResampler  # count formula constant only
from .ratios import RationalRatio


def _next_fast_len(n: int) -> int:
    """Smallest 2,3,5-smooth number >= n (good FFT length)."""
    size = max(n, 1)
    while True:
        v = size
        for p in (2, 3, 5):
            while v % p == 0:
                v //= p
        if v == 1:
            return size
        size += 1


def fft_convolution_full(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Linear convolution via one padded circular FFT (full length)."""
    n = a.size + b.size - 1
    nfft = _next_fast_len(n)
    fa = np.fft.rfft(a, nfft)
    fb = np.fft.rfft(b, nfft)
    conv = np.fft.irfft(fa * fb, nfft)
    return conv[:n]


def reference_resample(x: np.ndarray, ratio: RationalRatio,
                       design: FilterDesign) -> np.ndarray:
    """Off-line reference output under the exact project convention.

    Returns ``y[0 .. N_out-1]`` with
    ``N_out = floor((L*(N-1)+H)/M)+1`` (0 samples when N == 0).
    """
    if x.ndim != 1:
        raise ValueError("reference input must be 1-D")
    n_in = x.size
    if n_in == 0:
        return np.empty(0, dtype=np.float64)
    l, m = ratio.l, ratio.m
    h = design.half

    xz = np.zeros(l * n_in, dtype=np.float64)
    xz[::l] = x
    conv = fft_convolution_full(xz, design.coeffs)
    # conv index: full convolution starts at -H relative to xz, so the
    # prototype-aligned high-rate sample yz[k] == conv[k + H].
    n_out = PolyphaseResampler.expected_output_count(n_in, ratio, design)
    n = np.arange(n_out, dtype=np.int64)
    y = conv[n * m + h]
    if not np.all(np.isfinite(y)):
        raise FloatingPointError("reference produced non-finite samples")
    return y


def resample_offline(x: np.ndarray, fin: int, fout: int, *,
                     attenuation_db: float = 80.0,
                     transition_half_width: float = 0.1,
                     max_taps: int = 2_000_001,
                     max_rate: int = 10_000_000,
                     max_factor: int = 4096):
    """Convenience: build ratio+filter and run the FFT reference."""
    ratio = RationalRatio.reduce(fin, fout, max_rate=max_rate,
                                 max_factor=max_factor)
    from .fir import design_prototype
    design = design_prototype(
        ratio, attenuation_db=attenuation_db,
        transition_half_width=transition_half_width, max_taps=max_taps)
    return reference_resample(np.asarray(x, dtype=np.float64), ratio, design), design
