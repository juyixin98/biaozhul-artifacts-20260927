"""可下载计划与连续播放边界。

计划条目带解析后的绝对字节范围与时间线起止;
连续播放边界来自时间线内核的 discontinuity 序号变化点。
"""

from __future__ import annotations

from .models import ContinuityBoundary, PlanEntry, PlaybackPlan, Playlist
from .timeline import build_timeline


def build_plan(playlist: Playlist) -> PlaybackPlan:
    tl = build_timeline(playlist)
    entries: list[PlanEntry] = []
    for i, seg in enumerate(playlist.segments):
        entries.append(
            PlanEntry(
                sequence=seg.sequence,
                uri=seg.uri,
                duration=seg.duration,
                timeline_start=float(tl.starts[i]),
                timeline_end=float(tl.starts[i] + tl.durations[i]),
                discontinuity=seg.discontinuity,
                discontinuity_sequence=seg.discontinuity_sequence,
                byte_range=seg.byte_range,
            )
        )

    boundaries: list[ContinuityBoundary] = []
    for idx in tl.boundary_indices():
        boundaries.append(
            ContinuityBoundary(
                index=int(idx),
                sequence=int(tl.sequences[idx]),
                timeline_offset=float(tl.starts[idx]),
                from_discontinuity_sequence=int(tl.discontinuity_sequences[idx - 1]),
                to_discontinuity_sequence=int(tl.discontinuity_sequences[idx]),
            )
        )

    diagnostics: list[str] = []
    if not entries:
        diagnostics.append("undecidable: 播放列表无分段,无法生成下载计划")
    if playlist.endlist:
        diagnostics.append("列表已 ENDLIST,计划为最终版本")
    else:
        diagnostics.append("列表未结束,计划可能随后续版本变化")

    return PlaybackPlan(
        entries=entries,
        boundaries=boundaries,
        total_duration=tl.total_duration,
        diagnostics=diagnostics,
    )
