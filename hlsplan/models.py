"""核心数据模型与失败类别定义。

媒体序号（media sequence）、discontinuity 序号与时间线在模型层即分离：
- Segment.media_sequence / Segment.discontinuity_sequence 各自独立维护；
- 时间线（起播时刻）不由模型存储，统一由 timeline 内核按时长推导。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class FailureCategory(str, Enum):
    """可归类的失败/异常类别，测试按类别断言。"""

    PARSE_ERROR = "PARSE_ERROR"
    DUPLICATE_TAG = "DUPLICATE_TAG"
    ENCRYPTION_UNSUPPORTED = "ENCRYPTION_UNSUPPORTED"
    BYTERANGE_UNRESOLVABLE = "BYTERANGE_UNRESOLVABLE"
    APPEND_AFTER_ENDLIST = "APPEND_AFTER_ENDLIST"
    WINDOW_REWIND = "WINDOW_REWIND"
    SEGMENT_RETRACTED = "SEGMENT_RETRACTED"
    SEGMENT_CONFLICT = "SEGMENT_CONFLICT"
    MISSING_SEGMENT = "MISSING_SEGMENT"
    NOT_FOUND = "NOT_FOUND"
    JOB_FAILED = "JOB_FAILED"


@dataclass(frozen=True)
class ByteRange:
    """已解析（绝对偏移）的字节范围。"""

    offset: int
    length: int

    @property
    def end(self) -> int:
        return self.offset + self.length

    def to_dict(self) -> dict:
        return {"offset": self.offset, "length": self.length}


@dataclass
class Segment:
    media_sequence: int
    discontinuity_sequence: int
    uri: str
    duration: float
    title: Optional[str] = None
    byte_range: Optional[ByteRange] = None
    discontinuity_before: bool = False
    gap: bool = False  # EXT-X-GAP：序号占位但分段不可用
    map_uri: Optional[str] = None
    map_byte_range: Optional[ByteRange] = None

    def to_dict(self) -> dict:
        return {
            "media_sequence": self.media_sequence,
            "discontinuity_sequence": self.discontinuity_sequence,
            "uri": self.uri,
            "duration": self.duration,
            "title": self.title,
            "byte_range": self.byte_range.to_dict() if self.byte_range else None,
            "discontinuity_before": self.discontinuity_before,
            "gap": self.gap,
            "map_uri": self.map_uri,
            "map_byte_range": self.map_byte_range.to_dict() if self.map_byte_range else None,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Segment":
        br = d.get("byte_range")
        mbr = d.get("map_byte_range")
        return cls(
            media_sequence=d["media_sequence"],
            discontinuity_sequence=d["discontinuity_sequence"],
            uri=d["uri"],
            duration=d["duration"],
            title=d.get("title"),
            byte_range=ByteRange(**br) if br else None,
            discontinuity_before=d.get("discontinuity_before", False),
            gap=d.get("gap", False),
            map_uri=d.get("map_uri"),
            map_byte_range=ByteRange(**mbr) if mbr else None,
        )


@dataclass
class PlaylistSnapshot:
    """一个播放列表版本的不可变解析结果。"""

    name: str
    media_sequence: int
    discontinuity_sequence: int
    target_duration: float
    endlist: bool
    segments: list = field(default_factory=list)  # list[Segment]
    playlist_type: Optional[str] = None
    version: int = 0
    source_hash: str = ""

    @property
    def first_sequence(self) -> Optional[int]:
        return self.segments[0].media_sequence if self.segments else None

    @property
    def last_sequence(self) -> Optional[int]:
        return self.segments[-1].media_sequence if self.segments else None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "media_sequence": self.media_sequence,
            "discontinuity_sequence": self.discontinuity_sequence,
            "target_duration": self.target_duration,
            "endlist": self.endlist,
            "playlist_type": self.playlist_type,
            "version": self.version,
            "source_hash": self.source_hash,
            "segments": [s.to_dict() for s in self.segments],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PlaylistSnapshot":
        return cls(
            name=d["name"],
            media_sequence=d["media_sequence"],
            discontinuity_sequence=d["discontinuity_sequence"],
            target_duration=d["target_duration"],
            endlist=d["endlist"],
            playlist_type=d.get("playlist_type"),
            version=d.get("version", 0),
            source_hash=d.get("source_hash", ""),
            segments=[Segment.from_dict(s) for s in d.get("segments", [])],
        )
