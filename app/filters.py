"""Streaming K-weighting filter bank (ITU-R BS.1770-4).

Two second-order IIR stages per channel, applied in the mandated order:
  Stage 1  high-shelf "pre-filter"  (boost ~+4 dB above ~1.5 kHz)
  Stage 2  RLB weighting high-pass  (second-order Butterworth shape, fc 38 Hz)

Implementation note
-------------------
The only numerical primitive imported is :func:`scipy.signal.lfilter`, called
per channel with carried transposed-direct-form-II state (``zi``/``zf``). The
coefficients (see :mod:`app.r128_constants`) are the published BS.1770-4
48 kHz values. Carrying ``zi`` across chunks is exactly what makes a feed of
small chunks bit-for-bit equivalent to one feed of the whole signal; that
property is asserted in ``tests/test_streaming_parity.py``.
"""
from __future__ import annotations

import numpy as np
from scipy.signal import lfilter

from .r128_constants import PRE_FILTER_A, PRE_FILTER_B, RLB_A, RLB_B


class StreamingKWeighting:
    """Stateful two-stage K-weighting filter for a fixed channel count."""

    def __init__(self, channels: int):
        if channels < 1:
            raise ValueError("channels must be >= 1")
        self.channels = channels
        b1 = np.asarray(PRE_FILTER_B, dtype=np.float64)
        a1 = np.asarray(PRE_FILTER_A, dtype=np.float64)
        b2 = np.asarray(RLB_B, dtype=np.float64)
        a2 = np.asarray(RLB_A, dtype=np.float64)
        # lfilter zi has order max(len(a), len(b)) - 1 = 2 delay elements.
        self._b1, self._a1, self._b2, self._a2 = b1, a1, b2, a2
        self.reset()

    def reset(self) -> None:
        self._zi1 = [np.zeros(2, dtype=np.float64) for _ in range(self.channels)]
        self._zi2 = [np.zeros(2, dtype=np.float64) for _ in range(self.channels)]

    def process(self, x: np.ndarray) -> np.ndarray:
        """Filter one chunk.

        Parameters
        ----------
        x : ndarray of shape (frames, channels), float64
            Input samples (PCM decoded to floating point).

        Returns
        -------
        ndarray
            K-weighted samples, same shape as ``x``.
        """
        if x.ndim != 2 or x.shape[1] != self.channels:
            raise ValueError(
                f"expected (frames, {self.channels}); got {x.shape}"
            )
        x = np.ascontiguousarray(x, dtype=np.float64)
        y = np.empty_like(x)
        for c in range(self.channels):
            stage1, zf1 = lfilter(self._b1, self._a1, x[:, c], zi=self._zi1[c])
            stage2, zf2 = lfilter(self._b2, self._a2, stage1, zi=self._zi2[c])
            self._zi1[c] = zf1
            self._zi2[c] = zf2
            y[:, c] = stage2
        return y
