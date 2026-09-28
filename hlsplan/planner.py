"""播放计划：把某个版本展开为可下载计划与连续播放边界。

计划条目保留真实 URI 与已解析的绝对字节范围（供下载器使用）；
诊断信息里的 URI 仍由 diagnostics 层脱敏。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from .diagnostics import DiagnosticLog
from .models import FailureCategory, PlaylistSnapshot
from .timeline import continuous_runs, find_missing_sequences


@dataclass
class PlanEntry:
    media_sequence: int
    discontinuity_sequence: int
    uri: str
    duration: float
    byte_range: Optional[dict]
    discontinuity_before: bool
    map_uri: Optional[str] = None
    map_byte_range: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "media_sequence": self.media_sequence,
            "discontinuity_sequence": self.discontinuity_sequence,
            "uri": self.uri,
            "duration": self.duration,
            "byte_range": self.byte_range,
            "discontinuity_before": self.discontinuity_before,
            "map_uri": self.map_uri,
            "map_byte_range": self.map_byte_range,
        }


@dataclass
class PlaybackPlan:
    name: str
    version: int
    entries: List[PlanEntry] = field(default_factory=list)
    runs: List[dict] = field(default_factory=list)          # 连续播放边界
    missing_sequences: List[int] = field(default_factory=list)
    endlist: bool = False
    total_duration: float = 0.0

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "version": self.version,
            "entries": [e.to_dict() for e in self.entries],
            "runs": self.runs,
            "missing_sequences": self.missing_sequences,
            "endlist": self.endlist,
            "total_duration": round(self.total_duration, 6),
        }


def build_plan(
    snapshot: PlaylistSnapshot,
    since_sequence: Optional[int] = None,
    log: Optional[DiagnosticLog] = None,
) -> PlaybackPlan:
    """生成下载/播放计划。

    since_sequence：只保留序号大于该值的分段（增量下载场景）。
    连续播放边界与缺段始终基于完整快照计算，不受增量过滤影响。
    """
    log = log or DiagnosticLog()

    segments = snapshot.segments
    if since_sequence is not None:
        segments = [s for s in segments if s.media_sequence > since_sequence]
    # GAP 占位分段不可下载，不进计划条目（在 missing_sequences 中体现）
    segments = [s for s in segments if not s.gap]

    entries = [
        PlanEntry(
            media_sequence=s.media_sequence,
            discontinuity_sequence=s.discontinuity_sequence,
            uri=s.uri,
            duration=s.duration,
            byte_range=s.byte_range.to_dict() if s.byte_range else None,
            discontinuity_before=s.discontinuity_before,
            map_uri=s.map_uri,
            map_byte_range=s.map_byte_range.to_dict() if s.map_byte_range else None,
        )
        for s in segments
    ]

    runs = continuous_runs(snapshot.segments)
    missing = find_missing_sequences(snapshot.segments)
    for seq in missing:
        log.warning(
            FailureCategory.MISSING_SEGMENT.value,
            "sequence gap inside window breaks continuous playback",
            media_sequence=seq,
        )

    plan = PlaybackPlan(
        name=snapshot.name,
        version=snapshot.version,
        entries=entries,
        runs=[r.to_dict() for r in runs],
        missing_sequences=missing,
        endlist=snapshot.endlist,
        total_duration=sum(r.end_time - r.start_time for r in runs),
    )
    log.info(
        "PLAN_BUILT",
        "playback plan built",
        entries=len(entries),
        runs=len(runs),
        missing=len(missing),
        endlist=snapshot.endlist,
    )
    return plan
