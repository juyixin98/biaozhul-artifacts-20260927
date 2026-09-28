"""Extended grapheme cluster segmentation.

Thin wrapper around the mature ``grapheme`` package (UAX #29, Unicode
13.0.0).  We deliberately do **not** guess cluster boundaries from
character counts: combining marks (``e + U+0301``), ZWJ emoji sequences and
regional-indicator flag pairs are all segmented through the library FSM.

The rest of the codebase consumes only :func:`cluster_spans` and
:func:`is_cluster_boundary`, so the dependency surface is tiny and could be
swapped without touching index/edit code.

Known upstream defect (grapheme 0.6.0): its FSM ``Prepend`` state returns
the ``default`` state instead of ``lf_or_control`` after consuming an LF or
CONTROL, so a sequence like ``U+0605 (Prepend) + LF + ZWJ`` wrongly keeps
the ZWJ attached to the LF's cluster.  UAX #29 rules GB3–GB5 give an
unconditional invariant independent of all other rules: *CR, LF and other
Control code points always form singleton clusters, except CR immediately
followed by LF which is one cluster* (GB4 breaks after them, GB5 before
them, and GB3/GB4/GB5 take precedence over GB9b Prepend).  We enforce that
invariant on the library output in :func:`_enforce_control_singletons` — a
narrow, spec-grounded correction, verified against the independent regex
``\\X`` oracle on hundreds of randomized strings.
"""

from __future__ import annotations

import grapheme

from .errors import SegmenterError
from .unicode_version import UNICODE_VERSION


def cluster_spans(text: str) -> list[tuple[int, int]]:
    """Half-open ``(codepoint_start, codepoint_end)`` spans of every cluster.

    Includes no empty spans: ``""`` -> ``[]``.
    """
    raw_spans: list[tuple[int, int]] = []
    start = 0
    try:
        for cluster in grapheme.graphemes(text):
            end = start + len(cluster)
            raw_spans.append((start, end))
            start = end
    except Exception as exc:  # the library raises plain ValueErrors/KeyErrors
        raise SegmenterError(f"{type(exc).__name__}: {exc}") from None
    if start != len(text):
        # Defensive: the iterator must consume exactly the input.
        raise SegmenterError(
            f"segmenter consumed {start}/{len(text)} codepoints"
        )
    return _enforce_control_singletons(text, raw_spans)


def _enforce_control_singletons(
    text: str, spans: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    """Apply the GB3–GB5 invariant inside each library-produced cluster.

    Splits any cluster that contains CR/LF/Control so that:
    * ``CR LF`` stays together (GB3, CR × LF);
    * every other CR, LF or Control becomes its own singleton (GB4, GB5).
    """
    fixed: list[tuple[int, int]] = []
    for s, e in spans:
        piece_start = s
        i = s
        while i < e:
            ch = text[i]
            cat = ord(ch)
            if cat == 0x0D and i + 1 < e and ord(text[i + 1]) == 0x0A:
                if piece_start < i:
                    fixed.append((piece_start, i))
                fixed.append((i, i + 2))  # CRLF one cluster (GB3)
                i += 2
                piece_start = i
            elif cat in (0x0D, 0x0A) or _is_control(ch):
                if piece_start < i:
                    fixed.append((piece_start, i))
                fixed.append((i, i + 1))  # singleton (GB4/GB5)
                i += 1
                piece_start = i
            else:
                i += 1
        if piece_start < e:
            fixed.append((piece_start, e))
    return fixed


def _is_control(ch: str) -> bool:
    """GCB=Control per the library's own property table (keeps data pinned)."""
    from grapheme.grapheme_property_group import (
        GraphemePropertyGroup as _G,
        get_group,
    )
    return get_group(ch) is _G.CONTROL


def gcb_group(ch: str) -> str:
    """Grapheme_Cluster_Break property name for one code point.

    Exposed for window widening in incremental edits; keeps the property
    lookup sourced from the same pinned library as segmentation.
    """
    from grapheme.grapheme_property_group import get_group
    return get_group(ch).name


def cluster_starts(text: str) -> list[int]:
    """Codepoint offsets that begin a cluster, followed by ``len(text)``."""
    starts = [s for s, _ in cluster_spans(text)]
    starts.append(len(text))
    return starts


def is_cluster_boundary(text: str, cp_offset: int) -> bool:
    """True iff ``cp_offset`` (0..len(text)) is a grapheme cluster boundary."""
    if not 0 <= cp_offset <= len(text):
        return False
    return cp_offset in _starts_set(text)


def _starts_set(text: str) -> set[int]:
    starts: set[int] = {0, len(text)}
    for s, _ in cluster_spans(text):
        starts.add(s)
    return starts
