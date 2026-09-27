"""EBU R128 K-weighting filter stage (Tech 3341 / BS.1770-4 stage 1).

Signal chain, in this exact order:
  1. RBJ high-shelf biquad: +4.0 dB gain, Q = 1/sqrt(2), f_c = 1500 Hz
  2. RBJ high-pass biquad: Q = 0.5, f_c = 38 Hz

Coefficients follow the RBJ "Audio EQ Cookbook" conventions, identical to the
widely used pyloudnorm reference. Filtering is performed with a streaming
direct-form IIR carrying its delay lines (``zi``/``zf``), so feeding a signal
in arbitrary chunks yields bit-identical output to filtering it in one call:

    out[:]  ==  concat(process(chunk) for chunk in chunks)

The filter is applied independently per channel *before* channel weighting and
block integration; zero initial conditions (signal assumed to start at rest).
"""

from __future__ import annotations

import numpy as np
from scipy.signal import lfilter

GAIN_HIGH_SHELF_DB = 4.0
Q_HIGH_SHELF = 1.0 / np.sqrt(2.0)
FC_HIGH_SHELF_HZ = 1500.0
Q_HIGH_PASS = 0.5
FC_HIGH_PASS_HZ = 38.0


def high_shelf_coeffs(gain_db: float = GAIN_HIGH_SHELF_DB,
                      q: float = Q_HIGH_SHELF,
                      fc_hz: float = FC_HIGH_SHELF_HZ,
                      sample_rate: int = 48000) -> tuple[np.ndarray, np.ndarray]:
    """RBJ high-shelf biquad numerator/denominator, normalized so a0 = 1."""
    a = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * np.pi * (fc_hz / sample_rate)
    alpha = np.sin(w0) / (2.0 * q)
    cos_w0 = np.cos(w0)
    sqrt_a = np.sqrt(a)

    b0 = a * ((a + 1) + (a - 1) * cos_w0 + 2 * sqrt_a * alpha)
    b1 = -2 * a * ((a - 1) + (a + 1) * cos_w0)
    b2 = a * ((a + 1) + (a - 1) * cos_w0 - 2 * sqrt_a * alpha)
    a0 = (a + 1) - (a - 1) * cos_w0 + 2 * sqrt_a * alpha
    a1 = 2 * ((a - 1) - (a + 1) * cos_w0)
    a2 = (a + 1) - (a - 1) * cos_w0 - 2 * sqrt_a * alpha
    return np.array([b0, b1, b2]) / a0, np.array([a0, a1, a2]) / a0


def high_pass_coeffs(q: float = Q_HIGH_PASS,
                     fc_hz: float = FC_HIGH_PASS_HZ,
                     sample_rate: int = 48000) -> tuple[np.ndarray, np.ndarray]:
    """RBJ high-pass biquad numerator/denominator, normalized so a0 = 1."""
    w0 = 2.0 * np.pi * (fc_hz / sample_rate)
    alpha = np.sin(w0) / (2.0 * q)
    cos_w0 = np.cos(w0)

    b0 = (1 + cos_w0) / 2
    b1 = -(1 + cos_w0)
    b2 = (1 + cos_w0) / 2
    a0 = 1 + alpha
    a1 = -2 * cos_w0
    a2 = 1 - alpha
    return np.array([b0, b1, b2]) / a0, np.array([a0, a1, a2]) / a0


def k_weighting_coeffs(sample_rate: int) -> tuple[tuple[np.ndarray, np.ndarray],
                                                  tuple[np.ndarray, np.ndarray]]:
    """Return ((b_shelf, a_shelf), (b_highpass, a_highpass)) for the rate."""
    return (high_shelf_coeffs(sample_rate=sample_rate),
            high_pass_coeffs(sample_rate=sample_rate))


class StreamingKWeighting:
    """Stateful per-channel K-weighting filter for streamed PCM chunks."""

    def __init__(self, sample_rate: int, num_channels: int):
        if num_channels < 1:
            raise ValueError("num_channels must be >= 1")
        (self._b_shelf, self._a_shelf), (self._b_hp, self._a_hp) = (
            k_weighting_coeffs(sample_rate))
        self.num_channels = num_channels
        self.reset()

    def reset(self) -> None:
        # lfilter delay lines, shape (filter_order, channels): zero rest state.
        self._zi_shelf = np.zeros((2, self.num_channels), dtype=np.float64)
        self._zi_hp = np.zeros((2, self.num_channels), dtype=np.float64)

    def process(self, samples: np.ndarray) -> np.ndarray:
        """Filter one chunk; ``samples`` has shape (n, channels) float64."""
        if samples.ndim != 2 or samples.shape[1] != self.num_channels:
            raise ValueError(
                f"expected shape (n, {self.num_channels}), got {samples.shape}")
        x = np.asarray(samples, dtype=np.float64)
        if x.shape[0] == 0:
            return x.copy()
        y, self._zi_shelf = lfilter(
            self._b_shelf, self._a_shelf, x, axis=0, zi=self._zi_shelf)
        y, self._zi_hp = lfilter(
            self._b_hp, self._a_hp, y, axis=0, zi=self._zi_hp)
        return y
