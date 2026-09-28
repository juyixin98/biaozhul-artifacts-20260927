"""Morton (Z-order / interleaved-bit) encoding and query-box decomposition.

Encoding rules (fixed per schema, immutable once data is written):
  * every dimension has a fixed bit width ``b_i``;
  * signed dimensions use the standard zig-zag bijection
    ``u = (v << 1) ^ (v >> (b-1))`` applied inside the ``b``-bit window
    (sign-magnitude style ordering: 0,-1,1,-2,2 ...), so negatives keep a
    well-defined monotone unsigned order;
  * unsigned dimensions map directly;
  * the Morton code interleaves dimension bits starting with the most
    significant bit of every dimension ("level 0 = all MSBs"). At a level
    ``l`` only dimensions with ``b_i > l`` contribute a bit; shorter
    dimensions are simply skipped (dead). Within a level the bit order is the
    dimension order ``0..n-1``. No high bit is ever truncated: the code is a
    Python ``int`` of up to ``sum(b_i)`` bits (hard cap ``MAX_TOTAL_BITS`` so
    the (hi, lo) uint64 pair persisted in chunks can hold it).

Query-box decomposition (``decompose_box``):
  The unsigned query box is covered by *prefix cells* (hyper-rectangles whose
  per-dimension side lengths are powers of two, aligned to those lengths). A
  cell at level ``l`` fixes the top ``l`` interleaved bit positions, so its
  Morton codes form one contiguous integer interval
  ``[prefix << free_bits, prefix << free_bits | (1 << free_bits) - 1]``.

  Cells fully inside the box emit *exact* intervals; cells partially
  overlapping emit *conservative* intervals (they must be residual-filtered);
  disjoint cells are dropped. Splitting proceeds best-first, always splitting
  the partial cell with the largest volume waste, until every overlapping
  cell is exact/leaf or the interval budget (slots) is exhausted. Budget
  exhaustion only ever *widens* candidates — partial cells are emitted
  conservatively whole, which can never drop a true hit.
"""
from __future__ import annotations

import heapq
from dataclasses import dataclass, field

MAX_TOTAL_BITS = 128
MIN_BITS = 1
MAX_BITS_SIGNED = 64    # raw value fits int64; zig-zag image fits uint64
MAX_BITS_UNSIGNED = 63  # raw coordinate is persisted in an int64 column


@dataclass(frozen=True)
class DimSpec:
    name: str
    bits: int
    signed: bool = True

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("dimension name must be a non-empty string")
        cap = MAX_BITS_SIGNED if self.signed else MAX_BITS_UNSIGNED
        if not (MIN_BITS <= self.bits <= cap):
            raise ValueError(
                f"dimension {self.name!r}: bits must be in [{MIN_BITS},{cap}] "
                f"for signed={self.signed}"
            )


@dataclass(frozen=True)
class SchemaSpec:
    dims: tuple[DimSpec, ...]
    total_bits: int = field(init=False)
    name: str = "default"

    def __post_init__(self) -> None:
        if not self.dims:
            raise ValueError("schema must declare at least one dimension")
        names = [d.name for d in self.dims]
        if len(set(names)) != len(names):
            raise ValueError("dimension names must be unique")
        for d in self.dims:
            d.validate()
        total = sum(d.bits for d in self.dims)
        if total > MAX_TOTAL_BITS:
            raise ValueError(
                f"total interleaved bits {total} exceed MAX_TOTAL_BITS={MAX_TOTAL_BITS}"
            )
        object.__setattr__(self, "total_bits", total)

    def dim_index(self, name: str) -> int:
        for i, d in enumerate(self.dims):
            if d.name == name:
                return i
        raise KeyError(name)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "dims": [{"name": d.name, "bits": d.bits, "signed": d.signed} for d in self.dims],
            "total_bits": self.total_bits,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "SchemaSpec":
        return cls(
            dims=tuple(DimSpec(d["name"], int(d["bits"]), bool(d.get("signed", True))) for d in payload["dims"]),
            name=payload.get("name", "default"),
        )


# --------------------------------------------------------------------------- #
# coordinate <-> unsigned mapping
# --------------------------------------------------------------------------- #
def to_unsigned(value: int, spec: DimSpec) -> int:
    """Map a raw (possibly signed) coordinate to its fixed-width unsigned code."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"coordinate for {spec.name!r} must be an int, got {type(value).__name__}")
    mask = (1 << spec.bits) - 1
    if spec.signed:
        lo = -(1 << (spec.bits - 1))
        hi = (1 << (spec.bits - 1)) - 1
        if not (lo <= value <= hi):
            raise ValueError(
                f"coordinate {value} for signed {spec.bits}-bit dimension {spec.name!r} "
                f"outside [{lo},{hi}]"
            )
        # zig-zag inside the b-bit window; no mask needed given range checks
        return ((value << 1) ^ (value >> (spec.bits - 1))) & mask
    if not (0 <= value <= mask):
        raise ValueError(
            f"coordinate {value} for unsigned {spec.bits}-bit dimension {spec.name!r} "
            f"outside [0,{mask}]"
        )
    return value


def from_unsigned(u: int, spec: DimSpec) -> int:
    """Inverse of :func:`to_unsigned`."""
    if spec.signed:
        return (u >> 1) ^ -(u & 1)
    return u


def encode(coords: tuple[int, ...] | list[int], schema: SchemaSpec) -> int:
    """Interleave raw coordinates into one Morton code (MSB-first per level)."""
    if len(coords) != len(schema.dims):
        raise ValueError(f"expected {len(schema.dims)} coordinates, got {len(coords)}")
    us = [to_unsigned(int(v), d) for v, d in zip(coords, schema.dims)]
    max_bits = max(d.bits for d in schema.dims)
    code = 0
    for level in range(max_bits):
        alive = [i for i, d in enumerate(schema.dims) if d.bits > level]
        for i in alive:
            bit = (us[i] >> (schema.dims[i].bits - 1 - level)) & 1
            code = (code << 1) | bit
    return code


def decode(code: int, schema: SchemaSpec) -> tuple[int, ...]:
    """Decode a Morton code back to raw coordinates (inverse of ``encode``)."""
    if code < 0:
        raise ValueError("Morton code must be non-negative")
    if code >> schema.total_bits:
        raise ValueError(
            f"Morton code {code} does not fit {schema.total_bits} interleaved bits"
        )
    n = len(schema.dims)
    max_bits = max(d.bits for d in schema.dims)
    us = [0] * n
    fixed = 0  # bits read so far
    total = schema.total_bits
    for level in range(max_bits):
        alive = [i for i, d in enumerate(schema.dims) if d.bits > level]
        for pos, i in enumerate(alive):
            # The bit for (level, i) sits at total_bits - 1 - (fixed + pos)
            bit = (code >> (total - 1 - fixed - pos)) & 1
            us[i] = (us[i] << 1) | bit
        # dimensions that ended earlier were already shifted at every prior
        # level; pad their trailing zeros once they become dead.
        fixed += len(alive)
    raw = tuple(from_unsigned(us[i], schema.dims[i]) for i in range(n))
    return raw


def split128(code: int) -> tuple[int, int]:
    """Split a <=128 bit code into (high uint64, low uint64) for storage."""
    return (code >> 64) & ((1 << 64) - 1), code & ((1 << 64) - 1)


def combine128(hi: int, lo: int) -> int:
    return ((int(hi) & ((1 << 64) - 1)) << 64) | (int(lo) & ((1 << 64) - 1))


# --------------------------------------------------------------------------- #
# box decomposition
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Interval:
    lo: int
    hi: int  # inclusive
    exact: bool

    def __post_init__(self) -> None:
        if self.lo > self.hi:
            raise ValueError(f"bad interval [{self.lo},{self.hi}]")


@dataclass
class DecompositionResult:
    intervals: tuple[Interval, ...]
    exact_intervals: int
    conservative_intervals: int
    budget: int
    budget_exhausted: bool
    levels_visited: int
    cells_split: int

    def predicate_intervals(self, exact: bool) -> tuple[Interval, ...]:
        return tuple(i for i in self.intervals if i.exact is exact)


@dataclass
class _Cell:
    """Prefix cell; ``bases``/``sides`` are unsigned per-dimension geometry."""

    level: int
    fixed_bits: int
    prefix: int
    bases: tuple[int, ...]
    sides: tuple[int, ...]


def _alive(schema: SchemaSpec, level: int) -> list[int]:
    return [i for i, d in enumerate(schema.dims) if d.bits > level]


def _cell_interval(cell: _Cell, total_bits: int) -> tuple[int, int]:
    free = total_bits - cell.fixed_bits
    lo = cell.prefix << free
    hi = lo | ((1 << free) - 1)
    return lo, hi


def _relation(
    bases: tuple[int, ...],
    sides: tuple[int, ...],
    box_lo: tuple[int, ...],
    box_hi: tuple[int, ...],
) -> str:
    """Return 'inside' | 'disjoint' | 'partial' of the cell vs. the box."""
    inside = True
    for blo, bside, qlo, qhi in zip(bases, sides, box_lo, box_hi):
        c_hi = blo + bside - 1
        if blo > qhi or c_hi < qlo:
            return "disjoint"
        if blo < qlo or c_hi > qhi:
            inside = False
    return "inside" if inside else "partial"


def _waste(bases: tuple[int, ...], sides: tuple[int, ...], box_lo: tuple[int, ...], box_hi: tuple[int, ...]) -> int:
    """Cell volume outside the box (partial cells only). Positive integer."""
    w = 1
    for blo, bside, qlo, qhi in zip(bases, sides, box_lo, box_hi):
        c_hi = blo + bside - 1
        w *= (min(c_hi, qhi) - max(blo, qlo) + 1)
    total = 1
    for s in sides:
        total *= s
    return total - w


def _children(cell: _Cell, schema: SchemaSpec, alive: list[int]) -> list[_Cell]:
    """Split one level deeper: each alive dimension gets one more fixed bit."""
    n = len(schema.dims)
    child_fixed = cell.fixed_bits + len(alive)
    kids: list[_Cell] = []
    m = len(alive)
    for mask in range(1 << m):
        new_bases = list(cell.bases)
        new_sides = list(cell.sides)
        prefix = cell.prefix
        for pos, i in enumerate(alive):
            bit = (mask >> (m - 1 - pos)) & 1
            half = cell.sides[i] >> 1
            base = cell.bases[i] + bit * half
            new_bases[i] = base
            new_sides[i] = half
            prefix = (prefix << 1) | bit
        kids.append(
            _Cell(
                level=cell.level + 1,
                fixed_bits=child_fixed,
                prefix=prefix,
                bases=tuple(new_bases),
                sides=tuple(new_sides),
            )
        )
    return kids


def _merge(intervals: list[Interval]) -> list[Interval]:
    """Merge adjacent intervals, but never merge exact with conservative."""
    if not intervals:
        return []
    intervals.sort(key=lambda iv: iv.lo)
    out = [intervals[0]]
    for iv in intervals[1:]:
        last = out[-1]
        if iv.exact == last.exact and iv.lo == last.hi + 1:
            out[-1] = Interval(last.lo, iv.hi, last.exact)
        else:
            out.append(iv)
    return out


def decompose_box(
    schema: SchemaSpec,
    box_lo: tuple[int, ...],
    box_hi: tuple[int, ...],
    max_intervals: int,
) -> DecompositionResult:
    """Cover an unsigned (inclusive) query box with Morton-code intervals.

    ``max_intervals`` is the hard budget on emitted intervals after merging.
    The result is always a *superset* cover: every code whose point lies in
    the box belongs to some interval. When the budget runs out the remaining
    partial cells are emitted as conservative (``exact=False``) whole-cell
    intervals and ``budget_exhausted`` is set.
    """
    n = len(schema.dims)
    if len(box_lo) != n or len(box_hi) != n:
        raise ValueError("box bounds must have one entry per dimension")
    for i, (lo, hi) in enumerate(zip(box_lo, box_hi)):
        if not (0 <= lo <= hi < (1 << schema.dims[i].bits)):
            raise ValueError(
                f"unsigned box bounds in dimension {schema.dims[i].name!r} invalid: [{lo},{hi}]"
            )
    if max_intervals < 1:
        raise ValueError("max_intervals must be >= 1")

    max_bits = max(d.bits for d in schema.dims)
    root = _Cell(
        level=0,
        fixed_bits=0,
        prefix=0,
        bases=tuple(0 for _ in range(n)),
        sides=tuple(1 << d.bits for d in schema.dims),
    )
    relation = _relation(root.bases, root.sides, box_lo, box_hi)
    if relation == "disjoint":
        return DecompositionResult((), 0, 0, max_intervals, False, 0, 0)

    emitted: list[Interval] = []
    # heap entries: (-waste, level, counter, cell); bigger waste split first.
    heap: list[tuple[int, int, int, _Cell]] = []
    serial = 0
    levels_visited = 0
    cells_split = 0

    def slots_taken() -> int:
        # After merging, the count is <= this; use the pre-merge count as the
        # conservative admission check.
        return len(emitted) + len(heap)

    def push_partial(cell: _Cell) -> None:
        nonlocal serial
        w = _waste(cell.bases, cell.sides, box_lo, box_hi)
        heapq.heappush(heap, (-w, cell.level, serial, cell))
        serial += 1

    if relation == "inside":
        lo, hi = _cell_interval(root, schema.total_bits)
        emitted.append(Interval(lo, hi, True))
    else:
        push_partial(root)

    while heap:
        _, level, _, cell = heapq.heappop(heap)
        levels_visited = max(levels_visited, level + 1)
        if level >= max_bits:
            lo, hi = _cell_interval(cell, schema.total_bits)
            emitted.append(Interval(lo, hi, True))  # single-point leaf
            continue
        alive = _alive(schema, level)
        fanout = 1 << len(alive)
        # Splitting replaces this cell's one future interval with up to
        # ``fanout``; admit only if the merged-result budget can plausibly
        # hold. If not, emit the cell conservatively (never drop it).
        if slots_taken() - 1 + fanout > max_intervals:
            lo, hi = _cell_interval(cell, schema.total_bits)
            emitted.append(Interval(lo, hi, False))
            continue
        cells_split += 1
        for kid in _children(cell, schema, alive):
            rel = _relation(kid.bases, kid.sides, box_lo, box_hi)
            if rel == "disjoint":
                continue
            if rel == "inside" or kid.level == max_bits:
                lo, hi = _cell_interval(kid, schema.total_bits)
                emitted.append(Interval(lo, hi, rel == "inside"))
                # a leaf that is 'partial' is a single point => actually exact
                if kid.level == max_bits and rel == "partial":
                    emitted[-1] = Interval(lo, hi, True)
            else:
                push_partial(kid)

    merged = _merge(emitted)
    exact = sum(1 for iv in merged if iv.exact)
    conservative = len(merged) - exact
    # Conservative intervals are only ever produced by the budget check, so
    # any of them proves the decomposition budget was exhausted.
    exhausted = conservative > 0
    return DecompositionResult(
        intervals=tuple(merged),
        exact_intervals=exact,
        conservative_intervals=conservative,
        budget=max_intervals,
        budget_exhausted=exhausted,
        levels_visited=levels_visited,
        cells_split=cells_split,
    )
