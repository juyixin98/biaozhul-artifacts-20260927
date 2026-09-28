"""Independent reference algorithms used ONLY by tests and verification.

Nothing in the production import graph imports this module: the kernel never
validates itself.  The references here are deliberately written differently
from the code they check:

* :func:`spread_interleave` builds codes with a per-bit *string-position*
  construction rather than the kernel's ``j * ndim + d`` arithmetic;
* :func:`naive_interval_cover` obtains its cover by testing every code word
  in the (small) domain — it has no recursion and no budget logic;
* :func:`naive_box_members` enumerates coordinates directly.
"""

from __future__ import annotations

from itertools import product


def spread_interleave(us: list[int], widths: list[int]) -> int:
    """Reference interleaving via explicit bit-position strings.

    Layout convention (shared with the kernel): bit j of dimension d lands at
    position ``j * ndim + d`` — dimension 0 owns the *low* slot of each row.
    Rows are emitted most-significant first, so within a row dimension d is
    written at string column ``ndim - 1 - d``.
    """
    wmax = max(widths)
    lines: list[str] = []
    for j in range(wmax - 1, -1, -1):
        chars = ["0"] * len(us)
        for d, (u, w) in enumerate(zip(us, widths)):
            if j < w:
                chars[len(us) - 1 - d] = str((u >> j) & 1)
        lines.append("".join(chars))
    return int("".join(lines), 2) if lines else 0


def deinterleave(code: int, widths: list[int]) -> list[int]:
    """Inverse of :func:`spread_interleave` built from the same string layout."""
    ndim = len(widths)
    wmax = max(widths)
    flat = format(code, f"0{wmax * ndim}b")
    values = [0] * ndim
    for j in range(wmax):
        row = flat[j * ndim:(j + 1) * ndim]  # MSB-first rows
        level = wmax - 1 - j
        for d, w in enumerate(widths):
            if level < w and row[ndim - 1 - d] == "1":
                values[d] |= 1 << level
    return values


def naive_box_members(box_unsigned: list[tuple[int, int]]) -> list[list[int]]:
    """Enumerate every unsigned coordinate inside a box."""
    axes = [range(lo, hi + 1) for lo, hi in box_unsigned]
    return [list(c) for c in product(*axes)]


def naive_code_members(widths: list[int], box_unsigned: list[tuple[int, int]]) -> list[int]:
    return sorted(spread_interleave(c, widths) for c in naive_box_members(box_unsigned))


def naive_interval_cover(
    widths: list[int], box_unsigned: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    """Minimal run-length cover obtained by scanning every code word."""
    total_bits = sum(widths) if widths else 0  # equal-width domains only
    members = set(naive_code_members(widths, box_unsigned))
    runs: list[tuple[int, int]] = []
    run_start = None
    for code in range(1 << total_bits):
        if code in members:
            if run_start is None:
                run_start = code
        elif run_start is not None:
            runs.append((run_start, code - 1))
            run_start = None
    if run_start is not None:
        runs.append((run_start, (1 << total_bits) - 1))
    return runs


def covers_members(intervals: list[tuple[int, int]], members: list[int]) -> bool:
    return sorted(any(lo <= c <= hi for lo, hi in intervals) for c in members) == \
        [True] * len(members)
