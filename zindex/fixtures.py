"""Deterministic synthetic fixtures (no external data / accounts).

All generators are seeded: the same (seed, n, shape) always yields the same
rows, so documented runs and test expectations are reproducible bit-for-bit.

Shapes:
    grid      - full Cartesian grid (small domains only)
    uniform   - independent uniform coordinates
    corners   - mass concentrated at box corners / signed extrema
    sparse_hd - high-dimensional sparse cloud on a few tight clusters
"""
from __future__ import annotations

from typing import Iterator

from .encoding import DimSpec, SchemaSpec


class Lcg:
    """Numerical Recipes LCG; deliberately independent of random.Random."""

    def __init__(self, seed: int) -> None:
        self.state = seed & 0xFFFFFFFFFFFFFFFF

    def next_u64(self) -> int:
        self.state = (self.state * 6364136223846793005 + 1442695040888963407) & 0xFFFFFFFFFFFFFFFF
        return self.state

    def below(self, n: int) -> int:
        return self.next_u64() % n


def _raw_range(dim: DimSpec) -> tuple[int, int]:
    if dim.signed:
        return -(1 << (dim.bits - 1)), (1 << (dim.bits - 1)) - 1
    return 0, (1 << dim.bits) - 1


def generate_rows(schema: SchemaSpec, n: int, shape: str = "uniform", seed: int = 1) -> list[tuple[int, ...]]:
    gen = {
        "uniform": _uniform,
        "corners": _corners,
        "sparse_hd": _sparse_hd,
        "grid": _grid,
    }
    if shape not in gen:
        raise ValueError(f"unknown fixture shape {shape!r}; choose from {sorted(gen)}")
    rows = list(gen[shape](schema, n, Lcg(seed)))
    if shape != "grid" and len(rows) != n:
        raise AssertionError("generator length bug")
    return rows


def _uniform(schema: SchemaSpec, n: int, rng: Lcg) -> Iterator[tuple[int, ...]]:
    bounds = [_raw_range(d) for d in schema.dims]
    for _ in range(n):
        yield tuple(lo + rng.below(hi - lo + 1) for lo, hi in bounds)


def _corners(schema: SchemaSpec, n: int, rng: Lcg) -> Iterator[tuple[int, ...]]:
    """Half the rows hug a box corner deterministically, half are uniform.

    Even-indexed rows are assigned round-robin to one of the hypercube's
    corners (small jitter), so data really is spatially concentrated instead
    of merely having extrema per dimension.
    """
    bounds = [_raw_range(d) for d in schema.dims]
    n_corners = 1 << len(schema.dims)
    for k in range(n):
        if k % 2 == 0:
            corner = (k // 2) % n_corners
            row = []
            for j, (lo, hi) in enumerate(bounds):
                edge = hi if (corner >> j) & 1 else lo
                jitter = max(1, (hi - lo) // 100)
                delta = rng.below(jitter + 1)
                row.append(edge - delta if edge == hi else edge + delta)
            yield tuple(row)
        else:
            yield tuple(lo + rng.below(hi - lo + 1) for lo, hi in bounds)


def _sparse_hd(schema: SchemaSpec, n: int, rng: Lcg) -> Iterator[tuple[int, ...]]:
    """A handful of tight clusters in high-dimensional space."""
    k_clusters = min(8, max(2, n // 64 or 2))
    bounds = [_raw_range(d) for d in schema.dims]
    centers = [
        tuple(lo + rng.below(hi - lo + 1) for lo, hi in bounds)
        for _ in range(k_clusters)
    ]
    radii = [max(1, (hi - lo) // 200) for lo, hi in bounds]
    for _ in range(n):
        c = centers[rng.below(k_clusters)]
        yield tuple(
            min(hi, max(lo, c[j] + (rng.below(2 * radii[j] + 1) - radii[j])))
            for j, (lo, hi) in enumerate(bounds)
        )


def _grid(schema: SchemaSpec, n: int, rng: Lcg) -> Iterator[tuple[int, ...]]:
    """Full Cartesian grid over a small centered window; ignores ``n``."""
    import itertools

    axis_values = []
    for d in schema.dims:
        lo, hi = _raw_range(d)
        width = min(hi - lo + 1, 4)  # 3/4-bit signed -> 4x4... grids of 16 points/dim
        start = -(width // 2) if d.signed else 0
        axis_values.append(list(range(start, start + width)))
    for row in itertools.product(*axis_values):
        yield tuple(int(v) for v in row)
