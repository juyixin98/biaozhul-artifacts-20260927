"""Candidate indexing and pruning lower bounds.

Two admissible lower bounds are applied, both valid under *non-negative*
edit costs:

1. Length bound (direction-aware)
   A candidate that is SHORTER than the query requires deleting the excess
   query chars (>= min_delete each); a LONGER one requires inserting the
   missing chars (>= min_insert each). The inclusive length window is
       [qlen - floor(t/min_delete), qlen + floor(t/min_insert)]
   (the relevant side degenerates to unbounded when that cost is 0).
   Using separate directional costs is tighter than a shared min_indel and
   remains admissible.

2. Frequency (character-multiset) bound
   Let ``d1`` be the L1 distance between the character frequency vectors of
   query and candidate. Every single edit operation changes the L1 distance
   by at most 2 (insert/delete move it by 1; substitution and transposition
   move it by at most 2). Any script therefore needs at least ``d1/2``
   operations, and each costs at least the global minimum edit cost
   ``min_edit`` — hence ``LB_freq = d1/2 * min_edit``.
   We intentionally do NOT use the sharper substitution-aware bound, because
   with asymmetric or zero costs that version is not provably admissible.

A candidate is dropped only when ``max(LB_len, LB_freq) > threshold``.
Anything at or below the bound is always scored, so pruning is lossless by
construction; tests assert this exhaustively.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from .config import CostModel, EPS
from .storage import VersionStore


def length_lower_bound(query_len: int, cand_len: int, costs: CostModel) -> float:
    # Direction-aware: excess query chars are deleted, missing ones inserted.
    delta = query_len - cand_len
    if delta >= 0:
        return delta * costs.min_delete
    return -delta * costs.min_insert


def frequency_lower_bound(
    q_counts: Counter, c_counts: dict[str, int], costs: CostModel
) -> float:
    # L1 distance between frequency vectors, computed without materializing
    # the symmetric difference.
    chars = set(q_counts) | set(c_counts)
    d1 = sum(abs(int(q_counts.get(ch, 0)) - int(c_counts.get(ch, 0))) for ch in chars)
    return 0.5 * d1 * costs.min_edit


def combined_lower_bound(
    query: str,
    cand_len: int,
    cand_counts: dict[str, int],
    costs: CostModel,
) -> float:
    q_counts = Counter(query)
    return max(
        length_lower_bound(len(query), cand_len, costs),
        frequency_lower_bound(q_counts, cand_counts, costs),
    )


@dataclass
class PruningStats:
    version_id: int
    threshold: float
    min_len: int
    max_len: int
    length_window_count: int      # rows the SQL length window contained
    retrieved: int               # rows actually pulled (after SQL limit)
    window_truncated: bool       # SQL limit cut the length window
    passed_bounds: int           # rows surviving both lower bounds
    bounds_rejected: int         # rows killed by combined lower bound


def candidate_bounds(query: str, threshold: float, costs: CostModel) -> tuple[int, int]:
    """Admissible inclusive length window (direction-aware costs)."""
    q = len(query)
    # Shorter candidates: deletions of query chars (unbounded if free delete).
    lo = 0 if costs.min_delete <= EPS else q - int(threshold / costs.min_delete + EPS)
    # Longer candidates: insertions (unbounded if free insert).
    hi = 10**9 if costs.min_insert <= EPS else q + int(threshold / costs.min_insert + EPS)
    return max(0, lo), hi


def retrieve_candidates(
    store: VersionStore,
    version_id: int,
    query: str,
    threshold: float,
    costs: CostModel,
    *,
    sql_limit: int,
) -> tuple[list[dict], PruningStats]:
    """Pull length-window rows then apply the frequency bound.

    Returns surviving candidate dicts (each with term/freq_counts/frequency)
    and stats explaining every funnel stage.
    """
    min_len, max_len = candidate_bounds(query, threshold, costs)
    window_count = store.count_in_window(version_id, min_len, max_len)
    truncated = window_count > sql_limit

    q_counts = Counter(query)
    survivors: list[dict] = []
    retrieved = 0
    rejected = 0
    for row in store.iter_candidates(version_id, min_len, max_len, limit=sql_limit):
        retrieved += 1
        lb_len = length_lower_bound(len(query), row["length"], costs)
        lb_freq = frequency_lower_bound(q_counts, row["freq_counts"], costs)
        if max(lb_len, lb_freq) > threshold + EPS:
            rejected += 1
            continue
        survivors.append(row)

    stats = PruningStats(
        version_id=version_id,
        threshold=threshold,
        min_len=min_len,
        max_len=max_len,
        length_window_count=window_count,
        retrieved=retrieved,
        window_truncated=truncated,
        passed_bounds=len(survivors),
        bounds_rejected=rejected,
    )
    return survivors, stats
