"""Video sample selection: presentation window, reference closure, pre-roll.

A trim window is defined on *presentation* time.  Decoding the presented
samples requires their reference pictures too, so the kept set is the
transitive reference closure of the presented window.  References that
fall outside the window become decode-only pre-roll samples; a reference
that the descriptor does not contain at all is a hard MISSING_REFERENCE
failure (never silently dropped).  Open-GOP streams are handled exactly
by this closure: leading pictures pull in references from before the
recovery point.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.errors import FailureCategory, PlannerError
from app.models import Stream


@dataclass(frozen=True)
class VideoSelection:
    decode_order: tuple[int, ...]  # kept sample indices, decode order
    preroll: frozenset[int]        # decode-only subset of decode_order
    presented: tuple[int, ...]     # presented subset, presentation order


def presentation_window_indices(stream: Stream, in_tick: int,
                                out_tick: int) -> list[int]:
    """Indices of samples whose presentation time lies in [in, out)."""
    return [s.index for s in stream.samples
            if in_tick <= s.pts < out_tick]


def reference_closure(stream: Stream, seeds: list[int] | set[int]) -> set[int]:
    """Transitive dependency closure over depends_on edges."""
    kept: set[int] = set()
    stack = list(seeds)
    n = len(stream.samples)
    while stack:
        idx = stack.pop()
        if idx in kept:
            continue
        if idx < 0 or idx >= n:
            raise PlannerError(
                FailureCategory.MISSING_REFERENCE,
                f"sample depends on index {idx} which the stream does not contain",
                {"stream": stream.stream_type, "missing_index": idx},
            )
        kept.add(idx)
        stack.extend(stream.samples[idx].depends_on)
    return kept


def select_video(stream: Stream, in_tick: int, out_tick: int) -> VideoSelection:
    """Select the samples needed to present [in_tick, out_tick)."""
    presented = presentation_window_indices(stream, in_tick, out_tick)
    if not presented:
        raise PlannerError(
            FailureCategory.EMPTY_WINDOW,
            f"no video samples presented in [{in_tick}, {out_tick})",
            {"in_tick": in_tick, "out_tick": out_tick},
        )
    closure = reference_closure(stream, presented)
    decode_order = tuple(sorted(closure))
    first = stream.samples[decode_order[0]]
    if not first.keyframe:
        raise PlannerError(
            FailureCategory.KEYFRAME_BOUNDARY,
            f"decode of the window starts at non-keyframe sample "
            f"{first.index}; re-encode is required",
            {"first_decode_index": first.index},
        )
    preroll = frozenset(closure.difference(presented))
    presented_order = tuple(sorted(presented, key=lambda i: stream.samples[i].pts))
    return VideoSelection(
        decode_order=decode_order,
        preroll=preroll,
        presented=presented_order,
    )
