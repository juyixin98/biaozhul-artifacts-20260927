"""Weighted edit distance with a returned edit path.

==============================================================================
EXPLICIT VARIANT DECISION — exactly one edit model, end to end
==============================================================================
The distance implemented here is the **weighted adjacent-transposition edit
distance**, defined directly as a shortest-path problem:

    nodes : strings
    edges : the four legal operations, applicable at every position
              * insert char at a boundary
              * delete a char
              * substitute a char
              * swap two ADJACENT chars  (cost may depend on the ordered pair,
                so transposition is asymmetric: swap("ei") and swap("ie") are
                different keys)

    distance(source, target) = minimum total weight of an operation script
                               that turns source into target.

This is the "unrestricted Damerau" *edit-script* semantics: transpositions
form chains and characters may participate repeatedly (``ca -> abc`` is one
swap plus one insertion = 2; the restricted OSA recurrence gives 3).

We deliberately do NOT implement the Lowrance-Wagner *table* recurrence.
Investigation during development showed the LW "last-occurrence crossed
block" recurrence equals this distance under unit costs but can MISVALUE
repeated-character swap chains under non-uniform/asymmetric swap weights
(e.g. costs swap(ab)=0.2, swap(ba)=1.5: the exact ``aab -> baa`` is two
adjacent swaps = 0.4, while LW yields 0.9 or 2.0). Mixing the formulations
would violate the single-variant requirement, so the shortest-path
definition is the implementation too.

ALGORITHM
    A* search over string states with an admissible heuristic
    ``h(s) = max(directional-length bound, 0.5 * L1(freq(s),freq(t)) *
    min_edit)``; the length bound charges excess chars at min_delete and
    missing chars at min_insert. Non-negative costs (validated in CostModel) make the
    heuristic consistent, so the first time the target is popped is optimal.
    Callers checking "distance <= threshold?" prune on ``f = g + h``. A
    node-expansion cap bounds the worst case and is surfaced as an explicit
    uncertainty rather than a guessed distance.

PATH SEMANTICS (important for the unrestricted model)
    Because a character may take part in several edits (swap chains!), the
    path is returned as an ORDERED LIST of steps applied to the evolving
    string, each addressing the string AS IT IS at that moment (``at`` =
    index before that step). Steps replay left-to-right:

      insert       {"op":"insert","at":i,"char":c}
      delete       {"op":"delete","at":i,"char":c}
      substitute   {"op":"substitute","at":i,"src":x,"dst":y}
      transpose    {"op":"transpose","at":i,"src":xy,"dst":yx}

    ``replay`` applies them in order and verifies each step's precondition;
    ``recompute_cost`` re-sums weights purely from the step list. Both audit
    the answer independently of the search.
==============================================================================
"""
from __future__ import annotations

import heapq
from collections import Counter
from dataclasses import dataclass
from typing import Literal

from .config import CostModel, EPS

OpType = Literal["insert", "delete", "substitute", "transpose"]


@dataclass(frozen=True)
class Operation:
    op: OpType
    at: int                     # index into the string AT THAT STEP
    char: str | None = None     # insert/delete character
    src: str | None = None      # substitute source char, or transpose pair
    dst: str | None = None      # substitute target char, or swapped pair

    def to_dict(self) -> dict:
        d: dict = {"op": self.op, "at": self.at}
        if self.op in ("insert", "delete"):
            d["char"] = self.char
        elif self.op == "substitute":
            d["src"] = self.src
            d["dst"] = self.dst
        else:  # transpose
            d["src"] = self.src
            d["dst"] = self.dst
        return d


class SearchCapExceeded(RuntimeError):
    """The node-expansion cap was hit before optimality was proven."""


@dataclass
class SearchResult:
    distance: float
    exact: bool
    operations: list[Operation]
    nodes_expanded: int


# --------------------------------------------------------------------------- #
# Admissible heuristic (identical bounds to dictionary pruning)
# --------------------------------------------------------------------------- #
def _heuristic(s: str, target_len: int, target_counts: Counter,
               costs: CostModel) -> float:
    # Direction-aware length bound: excess chars must be DELETED (>= min
    # delete), missing chars must be INSERTED (>= min insert). This stays
    # tight when one of the two directions is free and the other is not.
    delta = len(s) - target_len
    length_lb = delta * costs.min_delete if delta >= 0 else -delta * costs.min_insert
    s_counts = Counter(s)
    d1 = 0
    for ch in set(s_counts) | set(target_counts):
        d1 += abs(s_counts.get(ch, 0) - target_counts.get(ch, 0))
    freq_lb = 0.5 * d1 * costs.min_edit
    return max(length_lb, freq_lb)


def _neighbors(s: str, target_chars: frozenset[str], costs: CostModel,
               len_cap: int):
    """Yield ``(next_state, step_tuple, edge_cost)``."""
    n = len(s)
    for i in range(n):
        ch = s[i]
        yield s[:i] + s[i + 1:], ("delete", i, ch), costs.delete_cost(ch)
        for r in target_chars:
            if r != ch:
                yield (
                    s[:i] + r + s[i + 1:],
                    ("substitute", i, ch, r),
                    costs.substitute_cost(ch, r),
                )
    if n < len_cap:
        for i in range(n + 1):
            for r in target_chars:
                yield s[:i] + r + s[i:], ("insert", i, r), costs.insert_cost(r)
    for i in range(n - 1):
        if s[i] == s[i + 1]:
            continue  # swapping identical chars is a useless no-op
        pair = s[i:i + 2]
        yield (
            s[:i] + s[i + 1] + s[i] + s[i + 2:],
            ("transpose", i, pair, pair[::-1]),
            costs.transpose_cost(pair[0], pair[1]),
        )


# --------------------------------------------------------------------------- #
# Core search
# --------------------------------------------------------------------------- #
def _search(
    source: str,
    target: str,
    costs: CostModel,
    *,
    threshold: float | None,
    node_cap: int,
    need_path: bool,
) -> SearchResult:
    if source == target:
        return SearchResult(0.0, True, [], 0)

    target_counts = Counter(target)
    target_chars = frozenset(target)
    len_cap = len(source) + len(target)
    # Always-feasible reference script: delete all, then insert all.
    feasible = (
        sum(costs.delete_cost(c) for c in source)
        + sum(costs.insert_cost(c) for c in target)
    )
    if threshold is None:
        effective_limit = feasible
        f_limit = None
    else:
        effective_limit = min(threshold, feasible)
        f_limit = threshold

    g: dict[str, float] = {source: 0.0}
    parents: dict[str, tuple[str, tuple]] = {}
    counter = 0
    open_heap: list[tuple[float, int, str]] = [
        (_heuristic(source, len(target), target_counts, costs), 0, source)
    ]
    nodes = 0

    while open_heap:
        _, _, state = heapq.heappop(open_heap)
        d = g[state]
        if state == target:
            ops = _rebuild(source, target, parents) if need_path else []
            return SearchResult(d, True, ops, nodes)
        nodes += 1
        if nodes > node_cap:
            raise SearchCapExceeded(
                f"expanded {node_cap} states without proving optimality "
                f"for {source!r}->{target!r}"
            )

        for nxt, step, w in _neighbors(state, target_chars, costs, len_cap):
            nd = d + w
            if nd > effective_limit + EPS:
                continue
            if f_limit is not None:
                f = nd + _heuristic(nxt, len(target), target_counts, costs)
                if f > f_limit + EPS:
                    continue
            if nd + EPS < g.get(nxt, float("inf")):
                g[nxt] = nd
                if need_path:
                    parents[nxt] = (state, step)
                counter += 1
                heapq.heappush(
                    open_heap,
                    (nd + _heuristic(nxt, len(target), target_counts, costs),
                     counter, nxt),
                )

    # Open set exhausted: under the given threshold no qualifying script exists.
    return SearchResult(float("inf"), True, [], nodes)


def _rebuild(source: str, target: str,
             parents: dict[str, tuple[str, tuple]]) -> list[Operation]:
    """Trace target -> source and emit ordered, precondition-checked steps."""
    steps: list[tuple] = []
    state = target
    while state != source:
        prev, step = parents[state]
        steps.append((prev, step))
        state = prev
    steps.reverse()

    ops: list[Operation] = []
    for prev, step in steps:
        kind = step[0]
        if kind == "insert":
            _, i, r = step
            ops.append(Operation("insert", at=i, char=r))
        elif kind == "delete":
            _, i, ch = step
            ops.append(Operation("delete", at=i, char=ch))
        elif kind == "substitute":
            _, i, x, y = step
            ops.append(Operation("substitute", at=i, src=x, dst=y))
        else:  # transpose
            _, i, pair, swapped = step
            ops.append(Operation("transpose", at=i, src=pair, dst=swapped))
    return ops


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def distance(source: str, target: str, costs: CostModel,
             *, node_cap: int = 200_000) -> float:
    """Exact weighted adjacent-transposition distance."""
    return _search(source, target, costs,
                   threshold=None, node_cap=node_cap, need_path=False).distance


def distance_with_path(
    source: str, target: str, costs: CostModel,
    *, threshold: float | None = None, node_cap: int = 200_000,
) -> SearchResult:
    """Distance + ordered replayable steps.

    With ``threshold`` set, ``distance == inf`` means "no script at or below
    the threshold exists" — an exact answer to the threshold question.
    """
    return _search(source, target, costs,
                   threshold=threshold, node_cap=node_cap, need_path=True)


# --------------------------------------------------------------------------- #
# Sequential replay and independent cost recomputation
# --------------------------------------------------------------------------- #
def replay(source: str, operations: list[Operation]) -> str:
    """Apply the ordered steps to the evolving string, checking preconditions."""
    s = source
    for op in operations:
        i = op.at
        if op.op == "insert":
            if not (0 <= i <= len(s)):
                raise ValueError(f"insert index {i} out of range")
            s = s[:i] + op.char + s[i:]
        elif op.op == "delete":
            if not (0 <= i < len(s)) or s[i] != op.char:
                raise ValueError(f"delete precondition failed at {i}: {op.char!r}")
            s = s[:i] + s[i + 1:]
        elif op.op == "substitute":
            if not (0 <= i < len(s)) or s[i] != op.src:
                raise ValueError(f"substitute precondition failed at {i}")
            s = s[:i] + op.dst + s[i + 1:]
        else:  # transpose
            if not (0 <= i < len(s) - 1) or s[i:i + 2] != op.src or op.dst != op.src[::-1]:
                raise ValueError(f"transpose precondition failed at {i}: "
                                 f"{op.src!r}->{op.dst!r}")
            s = s[:i] + op.dst + s[i + 2:]
    return s


def recompute_cost(operations: list[Operation], costs: CostModel) -> float:
    """Sum step costs independently of the search."""
    total = 0.0
    for op in operations:
        if op.op == "insert":
            total += costs.insert_cost(op.char)
        elif op.op == "delete":
            total += costs.delete_cost(op.char)
        elif op.op == "substitute":
            total += costs.substitute_cost(op.src, op.dst)
        else:  # transpose
            total += costs.transpose_cost(op.src[0], op.src[1])
    return total
