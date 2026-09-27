"""Independent reference three-way merger, used ONLY by tests.

A small, separately written implementation of the classic *diff3*
line-aligned walk.  It imports nothing from :mod:`merge3` and exists so the
test suite cross-checks conflict-free answers against an oracle the core
cannot agree with by construction.

How it works
------------
1. Independently LCS-align *base* with *local* and with *remote* (whole
   line tokens, terminators attached) via a textbook dynamic program.
2. Merge the two alignments into one sequence of ``stable`` (aligned on all
   three) and ``unstable`` (changed by at least one side) regions, using
   the standard diff3 synchronization method.
3. For each region:
   * stable: emit it;
   * unstable, only one side changed: emit that side's text;
   * unstable, both sides changed identically: emit it once;
   * unstable, both sides changed differently: ``conflict``.

It only produces an answer for conflict-free inputs; on a conflict it
returns ``conflict=True`` so the test can instead verify the engine's
explicit-resolution rebuild (which the reference is not asked to judge).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


def split_keep(text: str) -> list[str]:
    out: list[str] = []
    buf = ""
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\r" and i + 1 < n and text[i + 1] == "\n":
            out.append(buf + "\r\n"); buf = ""; i += 2
        elif ch in ("\n", "\r"):
            out.append(buf + ch); buf = ""; i += 1
        else:
            buf += ch; i += 1
    if buf:
        out.append(buf)
    return out


def _lcs_maps(a: list[str], b: list[str]):
    """Return (a->b mapping, b->a mapping) for one LCS alignment."""
    n, m = len(a), len(b)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            dp[i][j] = dp[i + 1][j + 1] + 1 if a[i] == b[j] \
                else max(dp[i + 1][j], dp[i][j + 1])
    pairs: list[tuple[int, int]] = []
    i = j = 0
    while i < n and j < m:
        if a[i] == b[j]:
            pairs.append((i, j)); i += 1; j += 1
        elif dp[i + 1][j] >= dp[i][j + 1]:
            i += 1
        else:
            j += 1
    return dict(pairs), {j: i for i, j in pairs}


@dataclass
class Region:
    # ranges on each document: [start, end)
    bs: int; be: int
    ls: int; le: int
    rs: int; re_: int
    stable: bool


def _sync_regions(base, local, remote):
    """Classic diff3 synchronization into stable/unstable regions."""
    lb, bl = _lcs_maps(base, local)   # base->local, local->base
    rb, br = _lcs_maps(base, remote)  # base->remote, remote->base

    regions: list[Region] = []
    i = j = k = 0  # cursors into base, local, remote
    n, m, q = len(base), len(local), len(remote)
    while i < n or j < m or k < q:
        # Find the next base index that is stable on both sides.
        stable_idx = None
        for t in range(i, n):
            if t in lb and t in rb:
                stable_idx = t
                break
        if stable_idx is None:
            if i < n or j < m or k < q:
                regions.append(Region(i, n, j, m, k, q, False))
            break
        lj = lb[stable_idx]
        rk = rb[stable_idx]
        # Everything before the stable line is an unstable region.
        if stable_idx > i or lj > j or rk > k:
            regions.append(Region(i, stable_idx, j, lj, k, rk, False))
        # The stable line itself.
        regions.append(Region(stable_idx, stable_idx + 1,
                              lj, lj + 1, rk, rk + 1, True))
        i, j, k = stable_idx + 1, lj + 1, rk + 1
    return regions


@dataclass
class ReferenceResult:
    merged: Optional[str]
    conflict: bool
    conflict_regions: tuple[tuple[int, int], ...]


def reference_merge(base_text: str, local_text: str,
                    remote_text: str) -> ReferenceResult:
    base = split_keep(base_text)
    local = split_keep(local_text)
    remote = split_keep(remote_text)
    regions = _sync_regions(base, local, remote)

    out: list[str] = []
    conflict_spans: list[tuple[int, int]] = []
    conflict = False

    for rg in regions:
        if rg.stable:
            out.append(base[rg.bs])
            continue
        l_chunk = local[rg.ls:rg.le]
        r_chunk = remote[rg.rs:rg.re_]
        b_chunk = base[rg.bs:rg.be]
        l_changed = l_chunk != b_chunk
        r_changed = r_chunk != b_chunk
        if not l_changed and not r_changed:
            out.extend(b_chunk)
        elif l_changed and not r_changed:
            out.extend(l_chunk)
        elif r_changed and not l_changed:
            out.extend(r_chunk)
        elif l_chunk == r_chunk:
            out.extend(l_chunk)          # identical independent change
        else:
            conflict = True
            conflict_spans.append((rg.bs, rg.be))

    return ReferenceResult(
        merged=None if conflict else "".join(out),
        conflict=conflict,
        conflict_regions=tuple(conflict_spans),
    )
