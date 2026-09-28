"""Streaming rational polyphase FIR resampler (mono float64 PCM).

Mathematical contract
=====================
Given coprime ``L`` (up) and ``M`` (down) and a symmetric prototype ``h`` of
length ``2H+1`` with DC gain ``L`` on the zero-stuffed stream::

    xz[k] = x[k/L]          if k % L == 0
            0               otherwise
    yz[k] = sum_q xz[k-q] * h[q]
    y[n]  = yz[n*M]

Only polyphase arm ``p = n*M mod L`` contributes to output ``n``; with
``k = n*M`` and ``q = p + j*L``::

    y[n] = sum_j x[k//L - j] * h[p + j*L],     -H <= p + j*L <= H

Padding / boundary strategy (fixed)
-----------------------------------
The prototype is a symmetric, acausal filter indexed ``-H .. H``.  We anchor
it with an explicit **fixed head pad** of ``P = H // L`` virtual zero input
samples before ``x[0]``, i.e. inputs are indexed ``x[-P..-1] = 0``.  Because
of that pad the output stream is *time aligned* with the input: an impulse at
input ``p`` (far from both boundaries) produces its peak at output
``floor(L*p/M)``.  The filter's intrinsic group delay is still real and is
reported separately (it is what the head pad compensates for).

* **Head** (fixed): ``P = H // L`` leading virtual zeros.  Streaming output is
  emitted from the first ``n`` whose whole arm lies on known samples, i.e. on
  the padded region plus received input; outputs whose taps would read beyond
  the available input are withheld.
* **Tail** (fixed): on ``flush`` the input is extended with
  ``Z = 2H//L + 2`` trailing virtual zeros, which always covers the longest
  backward tap; all remaining full-convolution outputs are then emitted.
* No windowing/fade is applied.  The transients at the very first and last
  outputs are therefore exactly those of a zero-padded symmetric FIR.

Group delay of the prototype (reported for downstream alignment) is ``H``
high-rate samples, i.e. ``H/L`` input samples, ``H/M`` output samples and
``H/(L*fin)`` seconds.

Deterministic sample count (``N_in >= 1``)::

    N_out = floor((L*(N_in - 1) + H) / M) + 1

and ``N_in == 0`` yields zero output.  The count and the values are
independent of how the input is split into chunks (bit-identical output).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ..errors import ComputationError, InvalidInputError, ResourceExhaustedError
from .fir import FilterDesign, design_prototype
from .ratios import RationalRatio


@dataclass
class PolyphaseResampler:
    """Chunk-at-a-time resampler with explicit :meth:`flush`.

    Parameters
    ----------
    fin, fout:
        Input/output sample rates in Hz (positive ints).
    output_dtype:
        ``"float64"`` (default) or ``"float32"``.  Accumulation is always
        float64; the cast to float32 is range-checked and raises
        :class:`ComputationError` on non-representable output.
    """

    fin: int
    fout: int
    output_dtype: str = "float64"
    attenuation_db: float = 80.0
    transition_half_width: float = 0.1
    max_taps: int = 2_000_001
    max_rate: int = 10_000_000
    max_factor: int = 4096
    max_input_chunk: int = 1_000_000
    ratio: RationalRatio = field(init=False)
    design: FilterDesign = field(init=False)
    _buffer: list[np.ndarray] = field(default_factory=list, init=False, repr=False)
    _buf_len: int = field(default=0, init=False)
    _base: int = field(default=0, init=False)
    _total_in: int = field(default=0, init=False)
    _n_next: int = field(default=0, init=False)
    _flushed: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if self.output_dtype not in {"float64", "float32"}:
            raise InvalidInputError(
                "output_dtype must be float64 or float32",
                details={"output_dtype": self.output_dtype},
            )
        self.ratio = RationalRatio.reduce(
            self.fin, self.fout,
            max_rate=self.max_rate, max_factor=self.max_factor,
        )
        self.design = design_prototype(
            self.ratio,
            attenuation_db=self.attenuation_db,
            transition_half_width=self.transition_half_width,
            max_taps=self.max_taps,
        )
        self.reset()

    # ------------------------------------------------------------------ state
    def reset(self) -> None:
        """Return the stream to its initial empty state."""
        self._buffer = []
        self._buf_len = 0
        self._base = 0
        self._total_in = 0
        self._n_next = 0
        self._flushed = False

    @property
    def flushed(self) -> bool:
        return self._flushed

    @property
    def total_input_samples(self) -> int:
        return self._total_in

    @property
    def total_output_samples(self) -> int:
        return self._n_next

    @property
    def head_pad_input_samples(self) -> int:
        """P: leading virtual zeros measured in input samples."""
        return self.design.half // self.ratio.l

    @property
    def tail_pad_input_samples(self) -> int:
        """Z: trailing virtual zeros appended at flush (input samples)."""
        h = self.design.half
        return 2 * h // self.ratio.l + 2

    # ----------------------------------------------------------------- helper
    @staticmethod
    def expected_output_count(n_in: int, ratio: RationalRatio,
                              design: FilterDesign) -> int:
        if n_in <= 0:
            return 0
        return (ratio.l * (n_in - 1) + design.half) // ratio.m + 1

    @staticmethod
    def _validate_input(x: np.ndarray, *, max_chunk: int) -> np.ndarray:
        if not isinstance(x, np.ndarray):
            raise InvalidInputError(
                "input must be a 1-D numpy array",
                details={"type": type(x).__name__},
            )
        if x.ndim != 1:
            raise InvalidInputError(
                "input must be one-dimensional (mono)",
                details={"ndim": x.ndim, "shape": list(x.shape)},
            )
        if x.dtype != np.float64:
            raise InvalidInputError(
                "input dtype must be float64",
                details={"dtype": str(x.dtype)},
            )
        if x.size > max_chunk:
            raise ResourceExhaustedError(
                f"chunk of {x.size} samples exceeds limit {max_chunk}",
                details={"chunk_samples": int(x.size), "limit": max_chunk},
            )
        bad = np.isfinite(x).argmin() if x.size else 0
        if x.size and not np.isfinite(x[bad]):
            raise InvalidInputError(
                "input contains non-finite sample",
                details={"index": int(bad), "value": float(x[bad])},
            )
        return x

    def _emit(self, extended: np.ndarray, base: int, n_first: int,
              n_last_inclusive: int) -> np.ndarray:
        """Compute outputs ``n_first .. n_last_inclusive`` against extended buf.

        ``extended`` must contain input samples for global indices starting at
        ``-head_pad`` (i.e. ``extended[head_pad + i - base] == x[i]``).
        """
        l, m = self.ratio.l, self.ratio.m
        h = self.design.half
        coeffs = self.design.coeffs
        n = np.arange(n_first, n_last_inclusive + 1, dtype=np.int64)
        k = n * m
        p = (k % l).astype(np.int64)
        center = k // l
        out = np.empty(n.size, dtype=np.float64)
        head = self.head_pad_input_samples
        # Every output is accumulated with np.einsum('i,i->') over a fixed
        # arm.  Plain np.dot dispatches to SIMD kernels whose reduction order
        # depends on the *memory alignment* of the operands, which changes as
        # chunk boundaries fall at different offsets — producing ~1 ULP
        # differences for the same logical multiply.  einsum uses a fixed
        # reduction for 1-D operands and (because fancy-indexed operands are
        # always freshly copied by the allocator) yields bit-identical
        # results regardless of chunking — an explicit project requirement.
        for row in range(n.size):
            phase = int(p[row])
            j0 = -((h + phase) // l)
            j1 = (h - phase) // l
            j = np.arange(j0, j1 + 1, dtype=np.int64)
            arm = coeffs[h + phase + j * l]
            idx = center[row] - j - base + head
            if idx[0] < 0 or idx[-1] >= extended.size:
                # Internal invariant: callers guarantee coverage.
                raise ComputationError(
                    "polyphase indexing out of buffer range",
                    details={"n": int(n[row]),
                             "idx_min": int(idx[0]),
                             "idx_max": int(idx[-1]),
                             "buffer_size": int(extended.size)},
                )
            samples = extended[idx]  # fancy gather: aligned copy, fixed kernel
            out[row] = np.einsum("i,i->", samples, arm)
        if not np.all(np.isfinite(out)):
            bad = int(np.isfinite(out).argmin())
            raise ComputationError(
                "non-finite output produced (overflow/NaN in accumulation)",
                details={"output_index": n_first + bad, "value": float(out[bad])},
            )
        if self.output_dtype == "float32":
            f32max = np.finfo(np.float32).max
            overflow = np.abs(out) > f32max
            if np.any(overflow):
                bad = int(np.flatnonzero(overflow)[0])
                raise ComputationError(
                    "output value not representable as float32",
                    details={"output_index": n_first + bad,
                             "value": float(out[bad])},
                )
            return out.astype(np.float32)
        return out

    # ----------------------------------------------------------------- stream
    def push(self, x: np.ndarray) -> np.ndarray:
        """Append one chunk; return every output whose taps are fully inside
        the known (non-padded) input."""
        if self._flushed:
            raise InvalidInputError(
                "stream already flushed; create a new job for more input",
                details={"state": "flushed"},
            )
        x = self._validate_input(x, max_chunk=self.max_input_chunk)
        if x.size == 0:
            return np.empty(0, dtype=np.dtype(self.output_dtype))
        self._buffer.append(x)
        self._buf_len += x.size
        self._total_in += x.size

        l, m = self.ratio.l, self.ratio.m
        h = self.design.half
        n_in = self._total_in
        # Streaming condition:
        #   k//L + (H + k%L)//L <= N_in - 1,  k = n*M
        n_candidate = self._n_next
        while True:
            k = n_candidate * m
            if k // l + (h + k % l) // l > n_in - 1:
                break
            n_candidate += 1
        n_end = n_candidate  # exclusive
        if n_end == self._n_next:
            return np.empty(0, dtype=np.dtype(self.output_dtype))

        base = self._base
        buf = np.concatenate(self._buffer)
        head = self.head_pad_input_samples
        extended = np.concatenate([np.zeros(head, dtype=np.float64), buf])
        out = self._emit(extended, base, self._n_next, n_end - 1)
        self._n_next = n_end
        self._prune(base, buf)
        return out

    def _prune(self, base: int, buf: np.ndarray) -> None:
        """Drop input samples no future output can reference."""
        l, m = self.ratio.l, self.ratio.m
        h = self.design.half
        k = self._n_next * m
        back = (h + k % l) // l
        center = k // l
        keep_from_global = center - back  # smallest index next emit may read
        if keep_from_global > base:
            cut = keep_from_global - base
            buf = buf[cut:]
            base += cut
        self._buffer = [buf] if buf.size else []
        self._buf_len = buf.size
        self._base = base

    def flush(self) -> np.ndarray:
        """Append the fixed tail of virtual zeros and emit the remainder."""
        if self._flushed:
            raise InvalidInputError(
                "stream already flushed",
                details={"state": "flushed"},
            )
        self._flushed = True
        n_in = self._total_in
        if n_in == 0:
            return np.empty(0, dtype=np.dtype(self.output_dtype))
        total = self.expected_output_count(n_in, self.ratio, self.design)
        if total == self._n_next:
            return np.empty(0, dtype=np.dtype(self.output_dtype))

        base = getattr(self, "_base", 0)
        buf = np.concatenate(self._buffer) if self._buffer else np.empty(0)
        head = self.head_pad_input_samples
        zeros = np.zeros(self.tail_pad_input_samples, dtype=np.float64)
        extended = np.concatenate([np.zeros(head, dtype=np.float64), buf, zeros])
        out = self._emit(extended, base, self._n_next, total - 1)
        self._n_next = total
        self._buffer = []
        self._buf_len = 0
        self._base = self._total_in
        return out

    # ------------------------------------------------------------------ specs
    def design_summary(self) -> dict:
        return {
            "fin": self.fin,
            "fout": self.fout,
            "l": self.ratio.l,
            "m": self.ratio.m,
            "high_rate": self.ratio.high_rate,
            "head_pad_input_samples": self.head_pad_input_samples,
            "tail_pad_input_samples": self.tail_pad_input_samples,
            "sample_count_rule": "n_out = floor((L*(n_in-1)+H)/M)+1 for n_in>=1",
            **self.design.design_summary(),
        }
