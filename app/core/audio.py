"""Audio sample selection: window overlap, encoder-delay priming, tail padding.

Audio frames are self-contained, so the kept set is every frame overlapping
the window, plus the decoder's encoder-delay priming frames whenever the
cut lies inside the priming region (those are marked ``priming``).  If the
presented audio ends before the window end, the tail is padded with
synthesised silence frames (``padding`` role) so the audio track covers
the video duration — the defined tail-padding behaviour of this planner.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from app.errors import FailureCategory, PlannerError
from app.models import Stream


@dataclass(frozen=True)
class PaddingFrame:
    dts: int
    pts: int
    duration: int


@dataclass(frozen=True)
class AudioSelection:
    kept: tuple[int, ...]          # real sample indices, decode order
    priming: frozenset[int]        # subset of kept: encoder-delay lead-in
    padding: tuple[PaddingFrame, ...]  # synthesised tail frames, source ticks


def select_audio(stream: Stream, in_tick: int, out_tick: int) -> AudioSelection:
    kept: list[int] = []
    priming: set[int] = set()
    # the decoder needs all encoder-delay frames before any frame it
    # actually presents; include them whenever the cut lies inside the
    # priming region (in_tick < encoder_delay)
    priming_required = in_tick < max(stream.encoder_delay, 1)
    for s in stream.samples:
        end = s.pts + s.duration
        if s.pts < out_tick and (end > in_tick or
                                 (priming_required and s.pts < in_tick)):
            kept.append(s.index)
            if s.pts < in_tick:
                priming.add(s.index)
    if not kept:
        raise PlannerError(
            FailureCategory.EMPTY_WINDOW,
            f"no audio samples overlap [{in_tick}, {out_tick})",
            {"in_tick": in_tick, "out_tick": out_tick},
        )
    presented_end = max(
        stream.samples[i].pts + stream.samples[i].duration for i in kept)
    padding: list[PaddingFrame] = []
    if presented_end < out_tick:
        last = stream.samples[kept[-1]]
        frame_dur = last.duration
        count = math.ceil((out_tick - presented_end) / frame_dur)
        for n in range(1, count + 1):
            padding.append(PaddingFrame(
                dts=last.dts + n * frame_dur,
                pts=presented_end + (n - 1) * frame_dur,
                duration=frame_dur,
            ))
    return AudioSelection(
        kept=tuple(kept),
        priming=frozenset(priming),
        padding=tuple(padding),
    )
