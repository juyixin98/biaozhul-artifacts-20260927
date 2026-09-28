"""Decompose a multi-dimensional query box into conservative Morton intervals.

Algorithm — *level-by-level Z-tree DFS* (one Morton bit per recursion level):

* a node wholly inside the box is emitted as one interval;
* a node wholly outside the box is skipped (its entire aligned subtree is
  pruned at once, which is what keeps the tree walk small for big boxes);
* a node straddling a face is split on exactly ONE interleaved bit at this
  level (``level % ndim`` picks the dimension), never on all dimensions at
  once — splitting on every dimension per level caused an exponential
  blow-up;
* leaves are single coordinates, tested against the box explicitly.

Unequal per-dimension widths: bits at level positions belonging to a
dimension already exhausted are phantom zeros — the walk descends through
them without splitting that dimension.

Budget semantics: after ``budget`` exact intervals have been emitted, a
partial node emits the conservative tail interval ``[lo, domain_hi]`` once.
Morton monotonicity guarantees every remaining true code is in that tail, so
exhaustion inflates candidates but cannot miss a row.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .coder import MortonCoder


@dataclass(frozen=True)
class Interval:
    """Inclusive Morton-code interval [lo, hi]."""

    lo: int
    hi: int
    exact: bool = True

    def __post_init__(self) -> None:
        if self.lo < 0 or self.hi < self.lo:
            raise ValueError(f"bad interval [{self.lo}, {self.hi}]")


@dataclass
class DecomposeResult:
    intervals: list[Interval]
    budget: int
    budget_exhausted: bool
    nodes_emitted: int
    merged_away: int
    box_unsigned: list[tuple[int, int]] = field(default_factory=list)

    @property
    def overapproximate_intervals(self) -> int:
        return sum(0 if iv.exact else 1 for iv in self.intervals)

    @property
    def is_exact(self) -> bool:
        return not self.budget_exhausted and all(iv.exact for iv in self.intervals)


def point_in_box_unsigned(ucoords: list[int], box: list[tuple[int, int]]) -> bool:
    return all(lo <= u <= hi for u, (lo, hi) in zip(ucoords, box))


def decompose_box(
    coder: MortonCoder,
    box_unsigned: list[tuple[int, int]],
    budget: int,
) -> DecomposeResult:
    if budget < 1:
        raise ValueError("interval budget must be >= 1")
    if len(box_unsigned) != coder.ndim:
        raise ValueError(f"box has {len(box_unsigned)} dims, coder has {coder.ndim}")
    blo: list[int] = []
    bhi: list[int] = []
    for d, (lo, hi) in zip(coder.dims, box_unsigned):
        if not (0 <= lo <= hi < (1 << d.bits)):
            raise ValueError(
                f"dimension {d.name}: box edge [{lo}, {hi}] outside {d.bits} "
                f"unsigned bits"
            )
        blo.append(lo)
        bhi.append(hi)

    ndim = coder.ndim
    widths = [d.bits for d in coder.dims]
    total_levels = coder.wmax * ndim
    domain_hi = (1 << coder.total_bits) - 1
    emitted: list[Interval] = []

    def free_mask(los: list[int], level: int) -> int:
        """Interleaved positions still free below ``level`` for each dim."""
        m = 0
        for d, w in enumerate(widths):
            fixed_layers = min(max(0, (level - d + ndim - 1) // ndim), w)
            for j in range(w - fixed_layers):
                m |= 1 << (j * ndim + d)
        return m

    def dfs(los: list[int], his: list[int], level: int) -> bool:
        # Whole subtree outside the box: prune it in O(1).
        if any(his[i] < blo[i] or los[i] > bhi[i] for i in range(ndim)):
            return True
        if all(los[i] == his[i] for i in range(ndim)):
            if point_in_box_unsigned(los, list(zip(blo, bhi))):
                code = coder.encode_unsigned(los)
                emitted.append(Interval(code, code, exact=True))
                return len(emitted) <= budget
            return True
        if all(los[i] >= blo[i] and his[i] <= bhi[i] for i in range(ndim)):
            lo_code = coder.encode_unsigned(los)
            hi_code = coder.encode_unsigned(his) | free_mask(los, level)
            emitted.append(Interval(lo_code, hi_code, exact=True))
            return len(emitted) <= budget

        if len(emitted) >= budget:
            # Conservative escape: cover the whole remaining code space.
            # A tighter tail would require proving no disjoint box region
            # remains below this node's lo (the walk is not strictly ordered
            # across sibling subtrees), so use the domain — inflate, never miss.
            emitted.append(Interval(0, domain_hi, exact=False))
            return False

        if level >= total_levels:
            emitted.append(Interval(0, domain_hi, exact=False))
            return False

        dim_idx = level % ndim
        layer = level // ndim  # MSB-first layer 0..w-1
        if layer >= widths[dim_idx]:
            return dfs(los, his, level + 1)

        half = 1 << (widths[dim_idx] - 1 - layer)
        mid = los[dim_idx] + half
        low_lo, low_hi = list(los), list(his)
        low_hi[dim_idx] = mid - 1
        high_lo, high_hi = list(los), list(his)
        high_lo[dim_idx] = mid
        for clo, chi in ((low_lo, low_hi), (high_lo, high_hi)):
            if not dfs(clo, chi, level + 1):
                return False
        return True

    root_lo = [0] * ndim
    root_hi = [(1 << w) - 1 for w in widths]
    if all(lo == 0 and hi == mx for (lo, hi), mx in
           zip(box_unsigned, root_hi)):
        return DecomposeResult([Interval(0, domain_hi, exact=True)],
                               budget, False, 1, 0,
                               [tuple(x) for x in zip(blo, bhi)])

    ok = dfs(root_lo, root_hi, 0)
    # On escape the whole-domain interval subsumes everything emitted so far;
    # return it alone so the candidate stage reads one clean inexact cover.
    if not ok:
        return DecomposeResult(
            [Interval(0, domain_hi, exact=False)],
            budget, True, len(emitted), 0,
            [tuple(x) for x in zip(blo, bhi)],
        )
    normalized, merged = _normalize(emitted)
    return DecomposeResult(
        normalized, budget, False, len(emitted), merged,
        [tuple(x) for x in zip(blo, bhi)],
    )


def _normalize(intervals: list[Interval]) -> tuple[list[Interval], int]:
    ordered = sorted(intervals, key=lambda iv: iv.lo)
    out: list[Interval] = []
    merged = 0
    for iv in ordered:
        if (out and iv.exact and out[-1].exact
                and iv.lo <= out[-1].hi + 1):
            out[-1] = Interval(out[-1].lo, max(out[-1].hi, iv.hi), exact=True)
            merged += 1
        else:
            out.append(iv)
    return out, merged
