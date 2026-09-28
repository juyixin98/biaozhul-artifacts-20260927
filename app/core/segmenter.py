"""Optimal segmentation via DAG shortest paths.

A standard left-to-right dynamic program over boundary vertices keeps the two
**distinct** best paths ending at every vertex. "Distinct" means different
token-surface tuples, so the runner-up gap is the gap to a genuinely different
segmentation, not a duplicate label.

Tie-breaking (exact rules for equal-cost paths):

    (total cost, token count, tuple of normalized token surfaces)

compared lexicographically, ascending on every component. Consequences:

1. fewer tokens wins at equal cost (prefer matching whole words over splits);
2. if the count is also equal, the lexicographically smaller surface sequence
   wins. Edge insertion order never affects the answer, so results are stable
   across dictionary build order and platforms.

Floating point sums are compared directly; the response exposes both the raw
gap and a rounded one for display.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from .dag import UNKNOWN_KIND, Edge, iter_dag_edges
from .normalize import NormalizedText, normalize_text
from .trie import Trie


@dataclass(frozen=True)
class Token:
    kind: str
    surface: str           # normalized substring
    display: str           # dictionary surface as published (same for OOV)
    cost: float
    norm_start: int
    norm_end: int
    orig_start: int
    orig_end: int

    @property
    def is_unknown(self) -> bool:
        return self.kind == UNKNOWN_KIND


@dataclass(frozen=True)
class PathView:
    surfaces: tuple[str, ...]
    cost: float
    token_count: int


@dataclass(frozen=True)
class SegmentationResult:
    text: str                          # original text
    normalized_text: str
    version: str
    request_id: str
    tokens: tuple[Token, ...]
    best: PathView
    runner_up: Optional[PathView]
    gap: Optional[float]
    gap_rounded: Optional[float]
    gap_status: str                    # "available" | "unique_path"
    orig_covered: bool                 # spans tile the original exactly
    reconstructed: bool                # concatenation of slices equals input
    unknown_tokens: int
    removed_chars: int


# --------------------------------------------------------------------------- #
# DP internals
# --------------------------------------------------------------------------- #

# Two paths composed of the *same* word costs in a different summation order
# can differ by a few ULPs (e.g. 7e-15 on costs around 35). Treating such
# noise as a real cost difference would make the documented tie rules depend
# on floating-point summation order. All ranking / band / tie decisions
# therefore use a quantized cost key; the exact float is still reported.
COST_EPS = 1e-9


def _cost_bucket(value: float) -> int:
    return int(round(value / COST_EPS))


@dataclass(frozen=True)
class _Label:
    """One candidate path ending at a vertex, with a back-pointer."""

    cost: float
    count: int
    surfaces: tuple[str, ...]
    edge: Edge              # last edge of this path
    parent: Optional["_Label"]  # label of the prefix path at edge.start

    @property
    def rank(self) -> tuple:
        return (_cost_bucket(self.cost), self.count, self.surfaces)

    @property
    def band(self) -> tuple[int, int]:
        return (_cost_bucket(self.cost), self.count)


_SOURCE = _Label(0.0, 0, (), Edge(0, 0, 0.0, "", "", ""), None)


class _BestTwo:
    """Labels at a vertex needed to recover global best and second-best.

    Plain "keep two labels" pruning is unsound under *ties*: at an interior
    vertex, two distinct paths may be exactly equal in ``(cost, token_count)``
    while differing in surface order, and either one can extend into the
    globally optimal or runner-up path downstream. We therefore keep every
    distinct label whose ``(cost, token_count)`` is no worse than the second
    distinct one -- i.e. all labels tied with the runner-up are retained.

    Surface tuples remain unique (same tuple = same segmentation).

    IMPORTANT: pruning happens once, after ALL incoming candidates for a
    vertex have been collected. Incremental pruning against a provisional
    second place can discard a candidate that only loses until a later edge
    at the same vertex is expanded.
    """

    __slots__ = ("labels",)

    def __init__(self) -> None:
        self.labels: list[_Label] = []

    def add_unpruned(self, candidate: _Label) -> None:
        """Collect a candidate, de-duplicating identical surface tuples."""
        for existing in self.labels:
            if existing.surfaces == candidate.surfaces:
                return
        self.labels.append(candidate)

    def prune(self) -> None:
        """Keep all labels holding 1st or 2nd place by (cost, token_count).

        Surface order only breaks ties in the *reported* answer; it never
        prunes here. Concretely we keep:

        * every label tied for 1st place on (cost, count), plus
        * every label tied for 2nd place (the cheapest strictly worse band)
          if a strictly-worse band exists.

        This preserves every interior prefix that could extend into a global
        optimum or runner-up, without retaining the full exponentially-sized
        path set.
        """
        if len(self.labels) <= 2:
            self.labels.sort(key=lambda lbl: lbl.rank)
            return
        ordered = sorted(self.labels, key=lambda lbl: lbl.rank)
        # Distinct quantized (cost, count) bands, cheapest first.
        bands: list[tuple[int, int]] = []
        for lbl in ordered:
            if not bands or bands[-1] != lbl.band:
                bands.append(lbl.band)
        keep_bands = {bands[0]}
        if len(bands) >= 2:
            keep_bands.add(bands[1])
        self.labels = [
            lbl for lbl in ordered
            if lbl.band in keep_bands
        ]


FORMAT_KIND = "format"


def _run_dp(text: str, edges: list[Edge]) -> dict[int, _BestTwo]:
    """Keep the two best distinct paths at every boundary vertex."""
    n = len(text)
    incoming: dict[int, list[Edge]] = {i: [] for i in range(n + 1)}
    for edge in edges:
        incoming[edge.end].append(edge)

    best: dict[int, _BestTwo] = {i: _BestTwo() for i in range(n + 1)}
    best[0].labels.append(_SOURCE)

    for v in range(1, n + 1):
        slot = best[v]
        for edge in incoming[v]:
            for tail in best[edge.start].labels:
                slot.add_unpruned(
                    _Label(
                        cost=tail.cost + edge.cost,
                        count=tail.count + 1,
                        surfaces=tail.surfaces + (edge.surface,),
                        edge=edge,
                        parent=tail,
                    )
                )
        # Prune only after every incoming edge for this vertex is seen.
        slot.prune()
    return best


def _trace(label: _Label) -> list[Edge]:
    """Recover the edge sequence by following parent back-pointers."""
    edges: list[Edge] = []
    node: Optional[_Label] = label
    while node is not None and node is not _SOURCE:
        edges.append(node.edge)
        node = node.parent
    edges.reverse()
    return edges


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def segment_text(
    source: str,
    trie: Trie,
    version: str,
    request_id: str,
    unknown_char_cost: float,
    max_word_length: int,
) -> tuple[SegmentationResult, NormalizedText]:
    """Segment ``source`` and return both the result and normalization data."""
    norm = normalize_text(source)
    n = norm.length

    edges = list(iter_dag_edges(norm.text, trie, unknown_char_cost, max_word_length))
    best = _run_dp(norm.text, edges)

    # ---------------------------------------------------------------- tokens
    if n == 0 and len(source) == 0:
        tokens: tuple[Token, ...] = ()
        best_view = PathView((), 0.0, 0)
        runner_up: Optional[PathView] = None
        gap: Optional[float] = None
        gap_status = "unique_path"
    elif n == 0:
        # Every input character normalized away (format-only text). Emit one
        # zero-width-in-normalized-space token covering the whole original
        # span, so the original is still covered exactly once.
        fmt = Token(
            kind=FORMAT_KIND,
            surface="",
            display="",
            cost=0.0,
            norm_start=0,
            norm_end=0,
            orig_start=0,
            orig_end=len(source),
        )
        tokens = (fmt,)
        best_view = PathView(("",), 0.0, 1)
        runner_up = None
        gap = None
        gap_status = "unique_path"
    else:
        labels = best[n].labels
        top = labels[0]
        best_edges = _trace(top)
        tokens = tuple(_build_token(edge, norm) for edge in best_edges)
        best_view = PathView(top.surfaces, top.cost, top.count)
        # Labels are kept sorted by the full rank (cost, count, surface
        # tuple), so labels[1] is the runner-up even when extra interior-tie
        # labels were retained at the final vertex.
        if len(labels) >= 2:
            second = labels[1]
            runner_up = PathView(second.surfaces, second.cost, second.count)
            gap = second.cost - top.cost
            gap_status = "available"
        else:
            runner_up = None
            gap = None
            gap_status = "unique_path"

    unknown_count = sum(1 for tok in tokens if tok.is_unknown)
    orig_covered, reconstructed = _check_coverage(source, norm, tokens)

    result = SegmentationResult(
        text=source,
        normalized_text=norm.text,
        version=version,
        request_id=request_id,
        tokens=tokens,
        best=best_view,
        runner_up=runner_up,
        gap=gap,
        gap_rounded=(round(gap, 6) if gap is not None and math.isfinite(gap) else None),
        gap_status=gap_status,
        orig_covered=orig_covered,
        reconstructed=reconstructed,
        unknown_tokens=unknown_count,
        removed_chars=norm.removed_chars,
    )
    return result, norm


def _build_token(edge: Edge, norm: NormalizedText) -> Token:
    orig_start, orig_end = norm.orig_span(edge.start, edge.end)
    return Token(
        kind=edge.kind,
        surface=edge.surface,
        display=edge.display,
        cost=edge.cost,
        norm_start=edge.start,
        norm_end=edge.end,
        orig_start=orig_start,
        orig_end=orig_end,
    )


def _check_coverage(source: str, norm: NormalizedText, tokens: tuple[Token, ...]) -> tuple[bool, bool]:
    """Spans must tile [0, len(source)); slices must concatenate to source."""
    if not tokens:
        covered = len(source) == 0
        return covered, covered
    expected = 0
    contiguous = True
    rebuilt_parts: list[str] = []
    for tok in tokens:
        if tok.orig_start != expected:
            contiguous = False
        expected = tok.orig_end
        rebuilt_parts.append(source[tok.orig_start : tok.orig_end])
    if expected != len(source):
        contiguous = False
    rebuilt = "".join(rebuilt_parts) == source
    return contiguous, rebuilt
