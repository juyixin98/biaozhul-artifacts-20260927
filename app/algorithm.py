"""DAG segmentation core: best and second-best word partition of a text.

The normalized text defines a directed acyclic graph:

* vertices ``0..n`` are character positions;
* an edge ``i -> j`` means emitting one token for ``text[i:j]``.
  Dictionary edges come from the trie; an unknown edge of exactly one
  character always exists, so every vertex sequence ``0,1,...,n`` is a path
  and **no character can ever be dropped**. A run of k unknown characters
  is simply k one-character edges (same cost as any grouping would have);
* the shortest path by summed token cost is the optimal segmentation.

Cost model
----------
* dictionary word: ``lexicon.word_cost`` (frequency derived);
* unknown character: ``unknown_char_cost`` per character, length explicit.

Two best *distinct* paths are computed with a k=2 k-shortest-paths DP over
the DAG, so the response reports best cost, second-best cost and their gap.
Ties never depend on dict/iteration order: equal-cost paths are ordered by a
fully content-based key (see ``_segment_paths``).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .lexicon import LexiconVersion
from .normalizer import NormalizedText, normalize

# Gap classification.
GAP_CLEAR = "clear"
GAP_CLOSE = "close"
GAP_NONE = "no_alternative"

# Diagnostics decisions.
DECISION_ACCEPTED = "accepted"
DECISION_INDETERMINATE = "indeterminate"


class TokenType(str, Enum):
    DICT = "dict"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Edge:
    start: int
    end: int
    surface: str
    cost: float
    kind: TokenType


@dataclass(frozen=True)
class Segment:
    surface: str  # normalized form as segmented
    type: str
    cost: float
    norm_start: int
    norm_end: int
    raw_start: int
    raw_end: int
    raw_text: str


@dataclass(frozen=True)
class SegmentationResult:
    normalized_text: str
    char_map: tuple[int, ...]
    deleted_raw_indices: tuple[int, ...]
    segments: tuple[Segment, ...]
    best_cost: float
    second_best_cost: float | None
    cost_gap: float | None
    gap_class: str
    decision: str
    tie_broken: bool
    # Diagnostics state (numbers only; content never logged from here).
    dict_edges: int
    unknown_edges: int
    vertices: int


# A complete path to vertex i is uniquely identified by
# (source-slot, incoming edge): the DAG has exactly one edge per
# (start, end, kind), and each stored slot is a distinct prefix.
#
# Equal-cost paths are ordered by the documented stable rule — lexicographic
# order of (surface, kind) token sequences, then fewer tokens. Each STORED
# slot (at most two per vertex) carries its ordering key tuple, built by
# extending its prefix slot's key. Cost ordering is the primary comparison;
# the content key only decides exact-cost ties and never touches iteration
# order. (Candidate keys that are never selected are discarded.)
@dataclass(frozen=True)
class _Slot:
    cost: float
    edge: Edge | None          # None only for the empty path at vertex 0
    prev_pos: int
    prev_rank: int             # rank of the chosen prefix in dp[prev_pos]
    key: tuple                 # (words_tuple, kinds_tuple, n_tokens)


_EMPTY_KEY: tuple = ((), (), 0)


def _segment_paths(
    text: str, lex: LexiconVersion, unknown_char_cost: float
) -> tuple[list[list[_Slot]], int, int]:
    """k=2 shortest *distinct* paths over the DAG, backpointer DP.

    Graph build and cost propagation are O(n * L) (candidate degree bounded
    by trie depth L), with two slots retained per vertex. The stable content
    key extends the stored prefix key by one token, so it is O(1) to append;
    on ordinary text costs almost always differ and the key is only compared
    for rare exact-cost ties, making the routine effectively linear. In a
    pathological input where many paths tie at every vertex, stored key
    length can grow quadratically — the service caps request length for that
    reason. The winning path is reconstructed once via backpointers.
    """
    n = len(text)
    from .lexicon import MAX_WORD_LEN_CAP

    scan_len = min(lex.trie.max_depth, MAX_WORD_LEN_CAP)
    incoming: list[list[Edge]] = [[] for _ in range(n + 1)]
    dict_edges = 0
    unknown_edges = 0
    for i in range(n):
        for e in lex.trie.prefix_matches(text, i, max_len=scan_len):
            word = text[i:e]
            incoming[e].append(Edge(i, e, word, lex.costs[word], TokenType.DICT))
            dict_edges += 1
        edge = Edge(i, i + 1, text[i], unknown_char_cost, TokenType.UNKNOWN)
        incoming[i + 1].append(edge)
        unknown_edges += 1

    dp: list[list[_Slot]] = [[] for _ in range(n + 1)]
    dp[0] = [_Slot(0.0, None, 0, 0, _EMPTY_KEY)]

    for i in range(1, n + 1):
        candidates: list[_Slot] = []
        for edge in incoming[i]:
            for rank, prefix in enumerate(dp[edge.start]):
                # Key starts empty; it is materialized only when an exact-cost
                # tie has to be broken or the slot is stored for descendants.
                candidates.append(
                    _Slot(prefix.cost + edge.cost, edge, edge.start, rank, _EMPTY_KEY)
                )

        candidates.sort(key=lambda s: s.cost)

        def materialize(slot: "_Slot") -> "_Slot":
            e = slot.edge
            assert e is not None
            pw, pk, pn = dp[e.start][slot.prev_rank].key
            kind_flag = 0 if e.kind is TokenType.DICT else 1
            return _Slot(
                slot.cost, e, e.start, slot.prev_rank,
                (pw + (e.surface,), pk + (kind_flag,), pn + 1),
            )

        # Within an equal-cost bucket the stable content key decides order;
        # keys are built lazily there (bucket size bounded by trie degree).
        ordered: list[_Slot] = []
        idx = 0
        while idx < len(candidates):
            j = idx + 1
            while j < len(candidates) and candidates[j].cost == candidates[idx].cost:
                j += 1
            bucket = candidates[idx:j]
            if len(bucket) > 1:
                bucket = sorted((materialize(s) for s in bucket),
                                key=lambda s: (s.cost, s.key))
            ordered.extend(bucket)
            idx = j

        chosen: list[_Slot] = []
        seen: set[tuple[int, int, int, TokenType]] = set()
        for cand in ordered:
            assert cand.edge is not None
            ident = (cand.prev_pos, cand.prev_rank, cand.edge.end, cand.edge.kind)
            if ident in seen:
                continue
            seen.add(ident)
            # Stored slots need a real key for descendants to extend.
            if cand.key is _EMPTY_KEY:
                cand = materialize(cand)
            chosen.append(cand)
            if len(chosen) == 2:
                break
        dp[i] = chosen
    return dp, dict_edges, unknown_edges


def _reconstruct_edges(dp: list[list["_Slot"]], pos: int, rank: int) -> list[Edge]:
    edges: list[Edge] = []
    while pos != 0:
        slot = dp[pos][rank]
        assert slot.edge is not None
        edges.append(slot.edge)
        pos, rank = slot.prev_pos, slot.prev_rank
    edges.reverse()
    return edges


def segment(
    raw_text: str,
    lex: LexiconVersion,
    *,
    unknown_char_cost: float = 20.0,
    close_gap_threshold: float = 1.0,
) -> SegmentationResult:
    """Segment raw text against an immutable lexicon version."""
    norm: NormalizedText = normalize(raw_text)
    text = norm.text
    n = len(text)

    dp, dict_edges, unknown_edges = _segment_paths(text, lex, unknown_char_cost)
    assert dp[n], "fallback edges guarantee a path for every input"
    best = dp[n][0]
    second = dp[n][1] if len(dp[n]) > 1 else None

    tie_broken = second is not None and best.cost == second.cost
    if second is None:
        gap = None
        gap_class = GAP_NONE
        decision = DECISION_ACCEPTED
    else:
        gap = second.cost - best.cost
        if gap < close_gap_threshold:
            gap_class = GAP_CLOSE
            decision = DECISION_INDETERMINATE
        else:
            gap_class = GAP_CLEAR
            decision = DECISION_ACCEPTED

    # Reconstruct the winning path once and merge adjacent unknown
    # one-character edges into one UNKNOWN segment so the reported fallback
    # length is explicit. Merging changes no costs.
    raw_len = len(raw_text)
    segments: list[Segment] = []
    edges = _reconstruct_edges(dp, n, 0)
    i = 0
    while i < len(edges):
        e = edges[i]
        if e.kind is TokenType.UNKNOWN:
            j = i
            cost = 0.0
            while j < len(edges) and edges[j].kind is TokenType.UNKNOWN:
                cost += edges[j].cost
                j += 1
            ns, ne = e.start, edges[j - 1].end
            segments.append(_make_segment(raw_text, raw_len, norm, text, ns, ne, cost, TokenType.UNKNOWN))
            i = j
        else:
            segments.append(
                _make_segment(raw_text, raw_len, norm, text, e.start, e.end, e.cost, TokenType.DICT)
            )
            i += 1

    return SegmentationResult(
        normalized_text=text,
        char_map=norm.char_map,
        deleted_raw_indices=norm.deleted_raw_indices,
        segments=tuple(segments),
        best_cost=best.cost,
        second_best_cost=None if second is None else second.cost,
        cost_gap=gap,
        gap_class=gap_class,
        decision=decision,
        tie_broken=tie_broken,
        dict_edges=dict_edges,
        unknown_edges=unknown_edges,
        vertices=n + 1,
    )


def _make_segment(
    raw_text: str,
    raw_len: int,
    norm: NormalizedText,
    text: str,
    ns: int,
    ne: int,
    cost: float,
    kind: TokenType,
) -> Segment:
    # Offset convention making raw coverage provably continuous: deleted raw
    # characters (e.g. soft hyphen) are absorbed into the segment *preceding*
    # their position; the first segment starts at 0 and the last ends at
    # len(raw_text).
    raw_start = 0 if ns == 0 else norm.char_map[ns]
    raw_end = raw_len if ne == len(text) else norm.char_map[ne]
    return Segment(
        surface=text[ns:ne],
        type=kind.value,
        cost=cost,
        norm_start=ns,
        norm_end=ne,
        raw_start=raw_start,
        raw_end=raw_end,
        raw_text=raw_text[raw_start:raw_end],
    )
