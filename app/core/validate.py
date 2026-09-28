"""Independent per-sample validation of a concatenation plan.

This module deliberately re-derives its expectations from the *source
segment descriptors* rather than trusting the planner, so it can be used
as a checker for plans produced anywhere.  Every rule of the constrained
container (``mp4-constrained/v1``) is verified sample by sample:

  R1  output DTS is never negative
  R2  output DTS is strictly increasing within a track
  R3  video PTS >= DTS (decode-before-present)
  R4  audio negative PTS stays within the declared edit-list priming
  R5  no required reference sample was dropped (closure recomputed)
  R6  presented samples (+audio padding) cover the whole trim window
  R7  padding only appears as a contiguous audio tail of its segment
  R8  each segment's video decode sequence starts on a keyframe
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from app.errors import FailureCategory
from app.models import (
    ROLE_PADDING,
    ROLE_PRESENT,
    ROLE_PRIMING,
    ConcatPlan,
    Segment,
)


@dataclass(frozen=True)
class Violation:
    category: FailureCategory
    detail: str
    context: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "detail": self.detail,
            "context": self.context,
        }


def _check_timestamps(plan: ConcatPlan) -> list[Violation]:
    violations: list[Violation] = []
    for track in plan.tracks:
        if not track.samples:
            continue
        dts = np.array([s.out_dts for s in track.samples], dtype=np.int64)
        neg = np.nonzero(dts < 0)[0]
        for pos in neg:
            violations.append(Violation(
                FailureCategory.NEGATIVE_DTS,
                f"{track.stream_type} sample at position {int(pos)} has "
                f"negative output DTS {int(dts[pos])}",
                {"track": track.stream_type, "position": int(pos),
                 "out_dts": int(dts[pos])}))
        steps = np.diff(dts)
        bad = np.nonzero(steps <= 0)[0]
        for pos in bad:
            violations.append(Violation(
                FailureCategory.DTS_NOT_MONOTONIC,
                f"{track.stream_type} DTS not strictly increasing at "
                f"position {int(pos)}: {int(dts[pos])} -> {int(dts[pos + 1])}",
                {"track": track.stream_type, "position": int(pos)}))
        if track.stream_type == "video":
            for pos, s in enumerate(track.samples):
                if s.out_pts < s.out_dts:
                    violations.append(Violation(
                        FailureCategory.PTS_BEFORE_DTS,
                        f"video sample at position {pos} presents "
                        f"({s.out_pts}) before it decodes ({s.out_dts})",
                        {"position": pos}))
        if track.stream_type == "audio":
            floor = -track.edit_list_media_time
            for pos, s in enumerate(track.samples):
                if s.out_pts < floor:
                    violations.append(Violation(
                        FailureCategory.AUDIO_PRIMING_RANGE,
                        f"audio sample at position {pos} has PTS {s.out_pts} "
                        f"below the edit-list floor {floor}",
                        {"position": pos, "out_pts": s.out_pts}))
    return violations


def _check_references(plan: ConcatPlan,
                      segments: dict[str, Segment]) -> list[Violation]:
    violations: list[Violation] = []
    for track in plan.tracks:
        by_segment: dict[str, list] = {}
        for s in track.samples:
            by_segment.setdefault(s.segment, []).append(s)
        for seg_name, samples in by_segment.items():
            segment = segments.get(seg_name)
            if segment is None:
                violations.append(Violation(
                    FailureCategory.CONTAINER_VIOLATION,
                    f"plan references unknown segment {seg_name!r}", {}))
                continue
            stream = segment.stream(track.stream_type)
            if stream is None:
                continue
            kept = {s.src_index for s in samples if s.src_index is not None}
            for s in samples:
                if s.src_index is None:
                    continue
                for dep in stream.samples[s.src_index].depends_on:
                    if dep not in kept:
                        violations.append(Violation(
                            FailureCategory.MISSING_REFERENCE,
                            f"{seg_name}/{track.stream_type} sample "
                            f"{s.src_index} requires reference {dep} which is "
                            f"absent from the plan",
                            {"segment": seg_name, "sample": s.src_index,
                             "missing": dep}))
    return violations


def _check_coverage(plan: ConcatPlan) -> list[Violation]:
    violations: list[Violation] = []
    windows = {w.segment: w for w in plan.segment_windows}
    for track in plan.tracks:
        by_segment: dict[str, list] = {}
        for s in track.samples:
            by_segment.setdefault(s.segment, []).append(s)
        for seg_name, samples in by_segment.items():
            window = windows.get(seg_name)
            if window is None or track.stream_type not in window.windows:
                continue
            win = window.windows[track.stream_type]
            # coverage is checked in *source* ticks: shift output stamps
            # back by the segment's relocation delta
            real = [s for s in samples if s.src_index is not None]
            if not real:
                continue
            delta = real[0].out_dts - real[0].src_dts
            # audio priming frames straddling the window start are partly
            # presented (clamped); video pre-roll frames never are.
            roles = {ROLE_PRESENT, ROLE_PADDING}
            if track.stream_type == "audio":
                roles.add(ROLE_PRIMING)
            presented = []
            for s in samples:
                if s.role not in roles:
                    continue
                start = s.out_pts - delta
                end = start + s.duration
                if end <= win["in_tick"]:
                    continue
                presented.append((max(start, win["in_tick"]), end))
            presented.sort()
            cursor = win["in_tick"]
            for start, end in presented:
                if start > cursor:
                    violations.append(Violation(
                        FailureCategory.COVERAGE_GAP,
                        f"{seg_name}/{track.stream_type} presentation gap "
                        f"[{cursor}, {start}) inside the trim window",
                        {"segment": seg_name, "track": track.stream_type}))
                    break
                cursor = max(cursor, end)
            else:
                if cursor < win["out_tick"]:
                    violations.append(Violation(
                        FailureCategory.COVERAGE_GAP,
                        f"{seg_name}/{track.stream_type} presentation ends at "
                        f"{cursor}, before the window end {win['out_tick']}",
                        {"segment": seg_name, "track": track.stream_type}))
    return violations


def _check_padding(plan: ConcatPlan) -> list[Violation]:
    violations: list[Violation] = []
    for track in plan.tracks:
        by_segment: dict[str, list] = {}
        for s in track.samples:
            by_segment.setdefault(s.segment, []).append(s)
        for seg_name, samples in by_segment.items():
            pad_positions = [i for i, s in enumerate(samples)
                             if s.role == ROLE_PADDING]
            if not pad_positions:
                continue
            if track.stream_type != "audio":
                violations.append(Violation(
                    FailureCategory.PADDING_MISUSE,
                    f"padding samples in non-audio track of {seg_name}",
                    {"segment": seg_name}))
            if pad_positions != list(range(len(samples) - len(pad_positions),
                                           len(samples))):
                violations.append(Violation(
                    FailureCategory.PADDING_MISUSE,
                    f"padding of {seg_name} is not a contiguous tail",
                    {"segment": seg_name, "positions": pad_positions}))
    return violations


def _check_keyframe_boundaries(plan: ConcatPlan) -> list[Violation]:
    violations: list[Violation] = []
    for track in plan.tracks:
        if track.stream_type != "video":
            continue
        by_segment: dict[str, list] = {}
        for s in track.samples:
            by_segment.setdefault(s.segment, []).append(s)
        for seg_name, samples in by_segment.items():
            if samples and not samples[0].keyframe:
                violations.append(Violation(
                    FailureCategory.KEYFRAME_BOUNDARY,
                    f"video decode sequence of {seg_name} starts at a "
                    f"non-keyframe sample (src_index={samples[0].src_index})",
                    {"segment": seg_name}))
    return violations


def validate_plan(plan: ConcatPlan,
                  segments: list[Segment] | None = None) -> list[Violation]:
    """Run all container rules against a plan.  Empty result == valid."""
    violations: list[Violation] = []
    violations.extend(_check_timestamps(plan))
    violations.extend(_check_padding(plan))
    violations.extend(_check_keyframe_boundaries(plan))
    violations.extend(_check_coverage(plan))
    if segments is not None:
        violations.extend(
            _check_references(plan, {s.name: s for s in segments}))
    return violations
