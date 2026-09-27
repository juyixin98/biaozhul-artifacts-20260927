"""数据模型。

设计要点:
- 媒体序号(media sequence)、discontinuity 序号、时间线位置是三个独立维度,
  分别由 Segment.sequence / Segment.discontinuity_sequence / timeline 内核维护,
  互不推导。
- 字节范围在解析期就解析为绝对 (length, offset),隐式偏移的继承在 parser 完成,
  下游不再处理相对偏移。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ByteRange:
    """已解析为绝对偏移的字节范围。"""

    length: int
    offset: int  # 相对所在 URI 起点的绝对偏移;隐式偏移已在解析期继承

    @property
    def end(self) -> int:
        return self.offset + self.length


@dataclass(frozen=True)
class Segment:
    sequence: int  # 媒体序号
    uri: str
    duration: float
    title: str = ""
    discontinuity: bool = False  # 该分段前是否有 EXT-X-DISCONTINUITY
    discontinuity_sequence: int = 0  # 该分段所属的 discontinuity 序号
    byte_range: ByteRange | None = None


@dataclass(frozen=True)
class Playlist:
    version: int
    target_duration: float
    media_sequence: int
    discontinuity_sequence: int
    playlist_type: str | None  # VOD / EVENT / None
    endlist: bool
    segments: tuple[Segment, ...]

    @property
    def first_sequence(self) -> int | None:
        return self.segments[0].sequence if self.segments else None

    @property
    def last_sequence(self) -> int | None:
        return self.segments[-1].sequence if self.segments else None

    def by_sequence(self) -> dict[int, Segment]:
        return {s.sequence: s for s in self.segments}


@dataclass(frozen=True)
class SegmentConflict:
    """同一已见媒体序号在新版本中 URI 或时长不一致。"""

    sequence: int
    field: str  # "uri" | "duration"
    old_value: str
    new_value: str


@dataclass
class CompareReport:
    """版本对比结果。decision: accept / conflict / reject。"""

    decision: str
    window_advanced: list[int] = field(default_factory=list)  # 窗口前移丢弃的序号
    retracted: list[int] = field(default_factory=list)  # 窗口内被撤回的内容序号
    appended: list[int] = field(default_factory=list)  # 新增序号
    conflicts: list[SegmentConflict] = field(default_factory=list)  # 冲突单列
    discontinuity_shift: tuple[int, int] | None = None  # (old, new) discontinuity 序号变化
    reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PlanEntry:
    sequence: int
    uri: str
    duration: float
    timeline_start: float
    timeline_end: float
    discontinuity: bool
    discontinuity_sequence: int
    byte_range: ByteRange | None


@dataclass(frozen=True)
class ContinuityBoundary:
    """连续播放边界:时间线上 discontinuity 序号发生变化的位置。"""

    index: int  # 边界处分段在计划中的下标
    sequence: int
    timeline_offset: float
    from_discontinuity_sequence: int
    to_discontinuity_sequence: int


@dataclass
class PlaybackPlan:
    entries: list[PlanEntry]
    boundaries: list[ContinuityBoundary]
    total_duration: float
    diagnostics: list[str] = field(default_factory=list)
