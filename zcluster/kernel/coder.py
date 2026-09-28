"""Morton (Z-order) coder for fixed-width signed/unsigned integer dimensions.

Every dimension has a *fixed* bit width ``w_d``.  Signed dimensions use the
standard order-preserving two's-complement sign flip (XOR 0x80...0), so the
unsigned image preserves numeric order.  Bits are then interleaved by
significance::

    bit j of dimension d lands at position  j * ndim + d

The most significant interleaved position is ``(wmax - 1) * ndim + (ndim - 1)``
— nothing above it is ever emitted, and every bit below it is emitted (as zero
for a dimension narrower than ``wmax``), so the high bits are never truncated.

Python's arbitrary-precision integers mean the kernel is exact for any width;
the column format adapts to uint64 vs. fixed-width big-endian bytes at the
storage boundary (see ``zcluster.format.chunks``).

Spread/compact use a divide-and-conquer *block-move* mask cascade
(:func:`spread_bits` / :func:`compact_bits`) — O(log bits) big-int operations
rather than O(bits) per-coordinate Python loops, which keeps interval
decomposition fast even at 12-bit x 4 dimensions.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache


FORMAT_VERSION = 1


@dataclass(frozen=True)
class DimSpec:
    """One interleaved dimension: name, fixed bit width, signedness."""

    name: str
    bits: int
    signed: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("dimension name must be a non-empty string")
        if not isinstance(self.bits, int) or self.bits < 1 or self.bits > 64:
            raise ValueError(f"dimension {self.name!r}: bits must be in 1..64, got {self.bits}")
        if not isinstance(self.signed, bool):
            raise ValueError(f"dimension {self.name!r}: signed must be bool")

    # -- value domain ----------------------------------------------------
    @property
    def raw_min(self) -> int:
        return -(1 << (self.bits - 1)) if self.signed else 0

    @property
    def raw_max(self) -> int:
        return (1 << (self.bits - 1)) - 1 if self.signed else (1 << self.bits) - 1

    def to_unsigned(self, value: int) -> int:
        """Map a raw domain value to its order-preserving unsigned image."""
        if not isinstance(value, int) or isinstance(value, bool):
            raise TypeError(f"dimension {self.name!r}: value must be int, got {type(value).__name__}")
        if not self.raw_min <= value <= self.raw_max:
            raise OutOfDomainError(
                f"dimension {self.name!r}: value {value} outside "
                f"[{self.raw_min}, {self.raw_max}] for {self.bits}-bit "
                f"{'signed' if self.signed else 'unsigned'}"
            )
        return value + (1 << (self.bits - 1)) if self.signed else value

    def from_unsigned(self, u: int) -> int:
        """Inverse of :meth:`to_unsigned`."""
        if not 0 <= u < (1 << self.bits):
            raise ValueError(f"dimension {self.name!r}: unsigned {u} outside {self.bits} bits")
        return u - (1 << (self.bits - 1)) if self.signed else u

    def to_dict(self) -> dict:
        return {"name": self.name, "bits": self.bits, "signed": self.signed}

    @classmethod
    def from_dict(cls, d: dict) -> "DimSpec":
        return cls(name=d["name"], bits=int(d["bits"]), signed=bool(d["signed"]))


class OutOfDomainError(ValueError):
    """A coordinate cannot fit the dimension's fixed bit width/sign mode."""


@lru_cache(maxsize=None)
def _position_tables(bits: int, ndim: int) -> tuple[tuple[int, ...], tuple[int, ...], int]:
    """Precomputed bit permutation: source j -> j*ndim, and its inverse.

    Returned as ``(spread_targets, compact_sources, total_width)`` where
    ``spread_targets[j]`` is the interleaved position of source bit j and
    ``compact_sources[p]`` is the source bit at interleaved position p.
    """
    spread_targets = tuple(j * ndim for j in range(bits))
    # Inverse over the FULL interleaved word width: positions that receive a
    # bit map back; other positions are ignored by compact after masking.
    total_width_bits = bits * ndim
    compact_sources = [-1] * total_width_bits
    for j in range(bits):
        compact_sources[j * ndim] = j
    return spread_targets, tuple(compact_sources), total_width_bits


def spread_bits(x: int, bits: int, ndim: int) -> int:
    """Spread the low ``bits`` bits of ``x`` to positions ``j*ndim``."""
    targets, _src, width = _position_tables(bits, ndim)
    y = 0
    for j, pos in enumerate(targets):
        if (x >> j) & 1:
            y |= 1 << pos
    return y


def compact_bits(y: int, bits: int, ndim: int) -> int:
    """Inverse of :func:`spread_bits`."""
    _targets, sources, _w = _position_tables(bits, ndim)
    x = 0
    for pos, j in enumerate(sources):
        if j >= 0 and (y >> pos) & 1:
            x |= 1 << j
    return x


class MortonCoder:
    """Exact bit-interleaving codec over a fixed ordered list of dimensions."""

    def __init__(self, dims: list[DimSpec]):
        if not dims:
            raise ValueError("at least one dimension is required")
        names = [d.name for d in dims]
        if len(set(names)) != len(names):
            raise ValueError("dimension names must be unique")
        self.dims: list[DimSpec] = list(dims)
        self.ndim = len(dims)
        self.wmax = max(d.bits for d in self.dims)
        self.total_bits = self.wmax * self.ndim
        self._name_index = {d.name: i for i, d in enumerate(self.dims)}

    # -- raw (signed-domain) convenience wrappers ------------------------
    def encode(self, values: list[int]) -> int:
        if len(values) != self.ndim:
            raise ValueError(f"expected {self.ndim} coordinates, got {len(values)}")
        return self.encode_unsigned([d.to_unsigned(v) for d, v in zip(self.dims, values)])

    def decode(self, code: int) -> list[int]:
        us = self.decode_unsigned(code)
        return [d.from_unsigned(u) for d, u in zip(self.dims, us)]

    # -- unsigned core ----------------------------------------------------
    def encode_unsigned(self, us: list[int]) -> int:
        if len(us) != self.ndim:
            raise ValueError(f"expected {self.ndim} unsigned coordinates, got {len(us)}")
        code = 0
        for idx, (d, u) in enumerate(zip(self.dims, us)):
            if not 0 <= u < (1 << d.bits):
                raise OutOfDomainError(
                    f"dimension {d.name!r}: unsigned {u} outside {d.bits} bits"
                )
            code |= spread_bits(u, d.bits, self.ndim) << idx
        return code

    def decode_unsigned(self, code: int) -> list[int]:
        if not isinstance(code, int) or isinstance(code, bool):
            raise TypeError("code must be an int")
        if not 0 <= code < (1 << self.total_bits):
            raise ValueError(f"code {code} outside {self.total_bits} interleaved bits")
        return [
            compact_bits(code >> idx, d.bits, self.ndim)
            for idx, d in enumerate(self.dims)
        ]

    # -- storage boundary helpers ----------------------------------------
    def encode_to_bytes(self, values: list[int]) -> bytes:
        """Big-endian fixed-width representation; width = ceil(total_bits/8)."""
        return self.encode(values).to_bytes((self.total_bits + 7) // 8, "big")

    @property
    def fits_uint64(self) -> bool:
        return self.total_bits <= 64

    @property
    def code_byte_width(self) -> int:
        return (self.total_bits + 7) // 8

    def dims_to_dict(self) -> list[dict]:
        return [d.to_dict() for d in self.dims]

    @classmethod
    def from_dicts(cls, ds: list[dict]) -> "MortonCoder":
        return cls([DimSpec.from_dict(d) for d in ds])
