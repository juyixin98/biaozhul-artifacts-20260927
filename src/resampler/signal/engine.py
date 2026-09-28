"""Streaming polyphase FIR resampling kernel.

Math contract (full derivation in docs/DESIGN.md)
-------------------------------------------------
With coprime L/M and normalized polyphase columns ``P[p, a]``
(shape L x K), output sample n is

    y[n] = sum_{a=0..K-1} P[p_n, a] * x_in[q_n - a]

where p_n = (n*M) mod L, q_n = floor(n*M/L), and x_in is the input
stream extended with K-1 leading zeros (fixed head padding).  The tail
uses K-1 trailing zeros, released by :meth:`flush`.

Output n is aligned with input time

    t(n) = (n*M - (K-1)/2) / (L*f_in)

so the kernel group delay is (K-1)/2 input samples.

Chunking invariance: the kernel never resamples block boundaries -- it
only retains the last K-1 inputs and the monotonically increasing output
counter n, therefore any partition of the input yields bit-identical
output samples and exactly ``plan.expected_outputs(J)`` samples overall.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from ..errors import ComputationError, StateConflictError
from .filter import ResamplePlan


class StreamingPolyphase:
    def __init__(self, plan: ResamplePlan):
        self._plan = plan
        self._L = plan.up
        self._M = plan.down
        self._K = plan.taps_per_phase
        self._P = plan.polyphase                       # (L, K)
        # Global history of length K-1 (x[J-K+1 .. J-1]); zeros until real data arrives.
        self._hist = np.zeros(self._K - 1, dtype=np.float64)
        self._J = 0                                   # input samples accepted
        self._next_n = 0                              # next output index to emit
        self._flushed = False
        self._out_count = 0

    @property
    def flushed(self) -> bool:
        return self._flushed

    @property
    def plan(self) -> ResamplePlan:
        return self._plan

    @property
    def inputs_seen(self) -> int:
        return self._J

    @property
    def outputs_emitted(self) -> int:
        return self._out_count

    def state_snapshot(self) -> dict[str, Any]:
        """Replay-relevant kernel state (used by the test/run logger)."""
        return {
            "up": self._L,
            "down": self._M,
            "taps_per_phase": self._K,
            "inputs_seen": self._J,
            "outputs_emitted": self._out_count,
            "next_output_index": self._next_n,
            "flushed": self._flushed,
            "hist_head": float(self._hist[0]) if self._K > 1 else 0.0,
            "hist_tail": float(self._hist[-1]) if self._K > 1 else 0.0,
            "hist_all_finite": bool(np.all(np.isfinite(self._hist))),
        }

    def _emit(self, seq: np.ndarray, first_global: int) -> np.ndarray:
        """Emit every output whose K-tap window lies inside ``seq``.

        ``seq[i]`` is global input sample ``first_global + i`` (zeros stand
        in for not-yet-arrived or padding samples).
        """
        last_global = first_global + seq.shape[0] - 1
        # Emit while the window's newest global index q(n)=floor(nM/L) <= last_global.
        # floor(nM/L) <= g  <=>  nM/L < g+1  <=>  n <= ceil(L*(g+1)/M) - 1.
        n1 = (self._L * (last_global + 1) + self._M - 1) // self._M - 1
        if self._next_n > n1:
            return np.empty(0, dtype=np.float64)
        n0 = self._next_n
        n = np.arange(n0, n1 + 1, dtype=np.int64)
        q = (n * self._M) // self._L                    # floor(nM/L)
        phase = ((n * self._M) % self._L).astype(np.intp)
        base_local = q - first_global                 # local index of global q

        # Gather windows: columns a = 0..K-1 hold global samples q-a.
        offsets = np.arange(self._K, dtype=np.int64)[None, :]
        idx = base_local[:, None] - offsets
        if idx.min() < 0 or idx.max() >= seq.shape[0]:
            # Defensive: index math must keep windows inside seq.
            raise ComputationError(
                "polyphase window index out of range",
                {"idx_min": int(idx.min()), "idx_max": int(idx.max()),
                 "seq_len": int(seq.shape[0])})
        windows = seq[idx]                            # (R, K)
        coeffs = self._P[phase]                       # (R, K)
        out = np.einsum("rk,rk->r", coeffs, windows, optimize=True)
        if not np.all(np.isfinite(out)):
            bad = int(np.sum(~np.isfinite(out)))
            raise ComputationError(
                "non-finite value produced by resampling kernel",
                {"bad_samples": bad, "first_bad_output": n0 +
                 int(np.argmax(~np.isfinite(out)))})
        self._next_n = int(n1) + 1
        self._out_count += out.shape[0]
        return out

    def push(self, block: np.ndarray) -> np.ndarray:
        """Feed one contiguous input block; return newly available outputs."""
        if self._flushed:
            raise StateConflictError(
                "input chunk received after flush",
                {"inputs_seen": self._J})
        arr = np.asarray(block, dtype=np.float64)
        if arr.ndim != 1:
            from ..errors import InputValidationError
            raise InputValidationError("input block must be 1-D mono PCM",
                                       {"ndim": arr.ndim})
        if arr.shape[0] == 0:
            return np.empty(0, dtype=np.float64)

        first_global = self._J - (self._K - 1)
        seq = np.concatenate((self._hist, arr))
        out = self._emit(seq, first_global)
        self._J += arr.shape[0]
        # Retain the final K-1 global samples of the assembled sequence.
        self._hist = seq[-(self._K - 1):].copy() if self._K > 1 else np.empty(0)
        return out

    def flush(self) -> np.ndarray:
        """Release the tail using K-1 fixed zero padding. Idempotent: no."""
        if self._flushed:
            raise StateConflictError("job flushed twice",
                                     {"inputs_seen": self._J})
        self._flushed = True
        if self._J == 0:
            return np.empty(0, dtype=np.float64)

        # Virtual trailing zeros: globals J .. J+K-2.
        tail = np.zeros(self._K - 1, dtype=np.float64)
        first_global = self._J - (self._K - 1)
        seq = np.concatenate((self._hist, tail))
        out = self._emit(seq, first_global)
        if self._out_count != self._plan.expected_outputs(self._J):
            raise ComputationError(
                "output count does not match plan after flush",
                {"emitted": self._out_count,
                 "expected": self._plan.expected_outputs(self._J),
                 "inputs": self._J})
        return out
