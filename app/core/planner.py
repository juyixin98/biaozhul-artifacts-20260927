"""Concatenation planner: compatibility, selection, timestamp relocation.

Pipeline per job:
  1. compatibility check  -> transcode_required with reasons, or continue
  2. per-segment sample selection (video closure / audio window + padding)
  3. timestamp relocation into a continuous per-track output timeline
  4. assembly of a sample-exact plan for the constrained container

Relocation shifts every kept sample of a segment by one integer delta per
track so that the first kept DTS lands on the running output cursor.  DTS
and PTS are shifted by the *same* delta, preserving decode-presentation
offsets (B-frame reorder) exactly.  The cursor starts at 0, so no output
DTS can be negative even when a source file uses negative DTS (common for
streams with B-frame reorder delay).
"""
from __future__ import annotations

import logging
from typing import Any

import numpy as np

from app.core import audio as audio_core
from app.core import gop as gop_core
from app.core.compat import check_segments_compatibility
from app.core.timebase import to_ticks
from app.models import (
    DECISION_DIRECT,
    DECISION_TRANSCODE,
    ROLE_PADDING,
    ROLE_PRESENT,
    ROLE_PREROLL,
    ROLE_PRIMING,
    ConcatPlan,
    PlanSample,
    Segment,
    SegmentRequest,
    SegmentWindow,
    TrackPlan,
)


def _resolve_window(segment: Segment, request: SegmentRequest,
                    stream_type: str) -> tuple[int, int]:
    """Resolve the trim window to integer ticks of this stream's time base."""
    stream = segment.stream(stream_type)
    assert stream is not None
    out_seconds = request.trim_out
    if out_seconds is None:
        out_seconds = segment.content_end_seconds()
    in_tick = to_ticks(request.trim_in, stream.tb,
                       what=f"trim_in of {segment.name}")
    out_tick = to_ticks(out_seconds, stream.tb,
                        what=f"trim_out of {segment.name}")
    if out_tick <= in_tick:
        from app.errors import FailureCategory, PlannerError
        raise PlannerError(
            FailureCategory.EMPTY_WINDOW,
            f"empty trim window [{in_tick}, {out_tick}) for {segment.name}",
            {"segment": segment.name})
    return in_tick, out_tick


def _relocate(entries: list[dict[str, Any]], cursor: int) -> int:
    """Shift a segment's kept samples onto the output timeline.

    ``entries`` carry ``src_dts``/``src_pts``; the function fills
    ``out_dts``/``out_pts`` in place and returns the new cursor.
    """
    first_dts = entries[0]["src_dts"]
    delta = cursor - first_dts
    for e in entries:
        e["out_dts"] = e["src_dts"] + delta
        e["out_pts"] = e["src_pts"] + delta
    ends = np.array([e["out_dts"] + e["duration"] for e in entries],
                    dtype=np.int64)
    return int(ends.max())


def build_concat_plan(
    segments: list[Segment],
    requests: list[SegmentRequest],
    container: str,
    log: logging.LoggerAdapter | None = None,
) -> ConcatPlan:
    """Build a sample-exact concatenation plan, or a transcode decision."""
    def _log(step: str, event: str, data: dict[str, Any] | None = None) -> None:
        if log is not None and hasattr(log, "log_step"):
            log.log_step(logging.INFO, step, event, data)

    if len(segments) != len(requests):
        raise ValueError("segments and requests must have the same length")

    # -- step 1: compatibility ------------------------------------------------
    problems = check_segments_compatibility(segments)
    _log("compat", "compatibility check finished",
         {"problems": [p.to_dict() for p in problems]})
    if problems:
        return ConcatPlan(
            container=container,
            decision=DECISION_TRANSCODE,
            reasons=[p.to_dict() for p in problems],
            tracks=[],
            segment_windows=[],
        )

    # -- step 2: per-segment selection ---------------------------------------
    windows: list[SegmentWindow] = []
    video_sel: list[gop_core.VideoSelection | None] = []
    audio_sel: list[audio_core.AudioSelection | None] = []
    for seg, req in zip(segments, requests):
        win: dict[str, dict[str, int]] = {}
        v_sel = a_sel = None
        if seg.stream("video") is not None:
            in_tick, out_tick = _resolve_window(seg, req, "video")
            v_sel = gop_core.select_video(seg.stream("video"), in_tick, out_tick)
            win["video"] = {"in_tick": in_tick, "out_tick": out_tick}
        if seg.stream("audio") is not None:
            in_tick, out_tick = _resolve_window(seg, req, "audio")
            a_sel = audio_core.select_audio(seg.stream("audio"), in_tick, out_tick)
            win["audio"] = {"in_tick": in_tick, "out_tick": out_tick}
        video_sel.append(v_sel)
        audio_sel.append(a_sel)
        windows.append(SegmentWindow(seg.name, seg.sha256, win))
        _log("select", f"selected samples for {seg.name}", {
            "segment": seg.name, "sha256": seg.sha256,
            "video_kept": list(v_sel.decode_order) if v_sel else None,
            "video_preroll": sorted(v_sel.preroll) if v_sel else None,
            "audio_kept": list(a_sel.kept) if a_sel else None,
            "audio_priming": sorted(a_sel.priming) if a_sel else None,
            "audio_padding": len(a_sel.padding) if a_sel else None,
        })

    # -- step 3+4: relocation and plan assembly -------------------------------
    tracks: list[TrackPlan] = []
    if any(s is not None for s in video_sel):
        ref_stream = next(s.stream("video") for s in segments
                          if s.stream("video") is not None)
        track = TrackPlan("video", ref_stream.codec, ref_stream.time_base, 0)
        cursor = 0
        for seg, sel in zip(segments, video_sel):
            if sel is None:
                continue
            stream = seg.stream("video")
            entries = []
            for idx in sel.decode_order:
                smp = stream.samples[idx]
                entries.append({
                    "segment": seg.name, "src_index": idx,
                    "src_dts": smp.dts, "src_pts": smp.pts,
                    "duration": smp.duration, "keyframe": smp.keyframe,
                    "idr": smp.idr,
                    "role": ROLE_PREROLL if idx in sel.preroll else ROLE_PRESENT,
                })
            cursor = _relocate(entries, cursor)
            for e in entries:
                track.samples.append(PlanSample(
                    segment=e["segment"], stream_type="video",
                    src_index=e["src_index"], src_dts=e["src_dts"],
                    src_pts=e["src_pts"], out_dts=e["out_dts"],
                    out_pts=e["out_pts"], duration=e["duration"],
                    keyframe=e["keyframe"], idr=e["idr"], role=e["role"]))
            _log("relocate", f"video track relocated for {seg.name}",
                 {"segment": seg.name, "cursor_after": cursor,
                  "first_out_dts": entries[0]["out_dts"]})
        tracks.append(track)

    if any(s is not None for s in audio_sel):
        ref_stream = next(s.stream("audio") for s in segments
                          if s.stream("audio") is not None)
        track = TrackPlan("audio", ref_stream.codec, ref_stream.time_base,
                          ref_stream.encoder_delay)
        cursor = 0
        for seg, sel in zip(segments, audio_sel):
            if sel is None:
                continue
            stream = seg.stream("audio")
            entries = []
            for idx in sel.kept:
                smp = stream.samples[idx]
                entries.append({
                    "segment": seg.name, "src_index": idx,
                    "src_dts": smp.dts, "src_pts": smp.pts,
                    "duration": smp.duration, "keyframe": False, "idr": False,
                    "role": ROLE_PRIMING if idx in sel.priming else ROLE_PRESENT,
                })
            for pad in sel.padding:
                entries.append({
                    "segment": seg.name, "src_index": None,
                    "src_dts": pad.dts, "src_pts": pad.pts,
                    "duration": pad.duration, "keyframe": False, "idr": False,
                    "role": ROLE_PADDING,
                })
            cursor = _relocate(entries, cursor)
            for e in entries:
                track.samples.append(PlanSample(
                    segment=e["segment"], stream_type="audio",
                    src_index=e["src_index"], src_dts=e["src_dts"],
                    src_pts=e["src_pts"], out_dts=e["out_dts"],
                    out_pts=e["out_pts"], duration=e["duration"],
                    keyframe=e["keyframe"], idr=e["idr"], role=e["role"]))
            _log("relocate", f"audio track relocated for {seg.name}",
                 {"segment": seg.name, "cursor_after": cursor,
                  "padding_frames": len(sel.padding)})
        tracks.append(track)

    return ConcatPlan(
        container=container,
        decision=DECISION_DIRECT,
        reasons=[],
        tracks=tracks,
        segment_windows=windows,
    )
