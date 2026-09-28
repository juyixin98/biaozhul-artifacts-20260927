"""HLS 媒体播放列表解析（RFC 8216 子集，限非加密分段媒体）。

职责边界：只做"文本 -> PlaylistSnapshot"的解析与结构校验，
不做版本间对比（compare.py）与时间线推导（timeline.py）。

关键规则：
- EXT-X-MEDIA-SEQUENCE / EXT-X-DISCONTINUITY-SEQUENCE 分别维护，互不推导；
- EXT-X-BYTERANGE 缺省偏移时，仅当上一分段是同一资源（URI 相同）的
  子范围时才继承其 end，否则报 BYTERANGE_UNRESOLVABLE；
- 单例标签重复出现 -> DUPLICATE_TAG（硬失败）；
- EXT-X-KEY METHOD != NONE -> ENCRYPTION_UNSUPPORTED（超出本服务范围）；
- EXT-X-MAP 的 BYTERANGE 缺省偏移按 0 处理（见 README 算法假设）。
"""
from __future__ import annotations

import hashlib
from typing import Optional

from .diagnostics import DiagnosticLog
from .models import (
    ByteRange,
    FailureCategory,
    PlaylistSnapshot,
    Segment,
)

# 出现两次即视为 DUPLICATE_TAG 的单例标签
_SINGLETON_TAGS = {
    "EXT-X-MEDIA-SEQUENCE",
    "EXT-X-DISCONTINUITY-SEQUENCE",
    "EXT-X-TARGETDURATION",
    "EXT-X-PLAYLIST-TYPE",
    "EXT-X-ENDLIST",
    "EXT-X-I-FRAMES-ONLY",
}


class ParseFailure(Exception):
    """解析硬失败；diagnostics 携带全部错误记录。"""

    def __init__(self, log: DiagnosticLog):
        self.log = log
        first = next((r.message for r in log.records if r.severity == "ERROR"), "parse failed")
        super().__init__(first)


def _parse_attribute_list(value: str) -> dict:
    """解析 key=value 逗号分隔属性表（引号内逗号不拆分）。"""
    attrs: dict = {}
    token, parts = [], []
    in_quote = False
    for ch in value:
        if ch == '"':
            in_quote = not in_quote
        if ch == "," and not in_quote:
            parts.append("".join(token))
            token = []
        else:
            token.append(ch)
    parts.append("".join(token))
    for part in parts:
        if "=" in part:
            k, v = part.split("=", 1)
            attrs[k.strip()] = v.strip().strip('"')
    return attrs


def _parse_byterange(value: str) -> tuple:
    """'length[@offset]' -> (length, offset|None)。"""
    if "@" in value:
        length_s, offset_s = value.split("@", 1)
        return int(length_s), int(offset_s)
    return int(value), None


def parse_playlist(
    text: str,
    name: str = "",
    log: Optional[DiagnosticLog] = None,
) -> PlaylistSnapshot:
    """解析媒体播放列表文本。硬失败抛 ParseFailure，软问题记入 log。"""
    log = log or DiagnosticLog()
    lines = [ln.strip() for ln in text.replace("\r\n", "\n").split("\n")]
    lines = [ln for ln in lines if ln]

    if not lines or lines[0] != "#EXTM3U":
        log.error(FailureCategory.PARSE_ERROR.value, "missing #EXTM3U header")
        raise ParseFailure(log)

    media_sequence = 0
    discontinuity_sequence = 0
    target_duration: Optional[float] = None
    playlist_type: Optional[str] = None
    endlist = False

    seen_singletons: dict = {}
    segments: list[Segment] = []
    pending_inf: Optional[tuple] = None  # (duration, title)
    pending_byterange: Optional[tuple] = None  # (length, offset|None)
    pending_discontinuity = False
    pending_gap = False
    map_uri: Optional[str] = None
    map_byterange: Optional[ByteRange] = None
    disc_offset = 0  # 已遇到的 EXT-X-DISCONTINUITY 个数

    for lineno, line in enumerate(lines[1:], start=2):
        if line.startswith("#"):
            body = line[1:]
            tag, _, value = body.partition(":")
            tag = tag.strip()
            value = value.strip()

            if tag in _SINGLETON_TAGS:
                if tag in seen_singletons:
                    log.error(
                        FailureCategory.DUPLICATE_TAG.value,
                        f"duplicate singleton tag {tag}",
                        tag=tag,
                        line=lineno,
                        first_line=seen_singletons[tag],
                    )
                    continue
                seen_singletons[tag] = lineno

            try:
                if tag == "EXT-X-MEDIA-SEQUENCE":
                    media_sequence = int(value)
                elif tag == "EXT-X-DISCONTINUITY-SEQUENCE":
                    discontinuity_sequence = int(value)
                elif tag == "EXT-X-TARGETDURATION":
                    target_duration = float(value)
                elif tag == "EXT-X-PLAYLIST-TYPE":
                    playlist_type = value
                elif tag == "EXT-X-ENDLIST":
                    endlist = True
                elif tag == "EXT-X-I-FRAMES-ONLY":
                    log.warning(
                        FailureCategory.PARSE_ERROR.value,
                        "EXT-X-I-FRAMES-ONLY playlists are out of scope; continuing",
                        line=lineno,
                    )
                elif tag == "EXT-X-KEY":
                    attrs = _parse_attribute_list(value)
                    method = attrs.get("METHOD", "NONE")
                    if method != "NONE":
                        log.error(
                            FailureCategory.ENCRYPTION_UNSUPPORTED.value,
                            f"encrypted media (METHOD={method}) is out of scope",
                            method=method,
                            line=lineno,
                            key_uri=attrs.get("URI"),
                        )
                elif tag == "EXT-X-MAP":
                    attrs = _parse_attribute_list(value)
                    map_uri = attrs.get("URI")
                    if "BYTERANGE" in attrs:
                        length, offset = _parse_byterange(attrs["BYTERANGE"])
                        # 假设：MAP 的 BYTERANGE 缺省偏移按 0（见 README）
                        map_byterange = ByteRange(offset=offset or 0, length=length)
                elif tag == "EXTINF":
                    if pending_inf is not None:
                        log.error(
                            FailureCategory.PARSE_ERROR.value,
                            "EXTINF without following segment URI",
                            line=lineno,
                        )
                        continue
                    dur_s, _, title = value.partition(",")
                    duration = float(dur_s)
                    if duration < 0:
                        log.error(
                            FailureCategory.PARSE_ERROR.value,
                            "negative EXTINF duration",
                            line=lineno,
                        )
                        continue
                    pending_inf = (duration, title or None)
                elif tag == "EXT-X-BYTERANGE":
                    pending_byterange = _parse_byterange(value)
                elif tag == "EXT-X-DISCONTINUITY":
                    pending_discontinuity = True
                    disc_offset += 1
                elif tag == "EXT-X-GAP":
                    pending_gap = True
                elif tag.startswith("EXT-X-") or tag.startswith("EXT"):
                    log.info("UNKNOWN_TAG", f"ignored tag {tag}", tag=tag, line=lineno)
            except ValueError as exc:
                log.error(
                    FailureCategory.PARSE_ERROR.value,
                    f"malformed {tag}: {exc}",
                    tag=tag,
                    line=lineno,
                )
            continue

        # 分段 URI 行
        if pending_inf is None:
            log.error(
                FailureCategory.PARSE_ERROR.value,
                "segment URI without preceding EXTINF",
                line=lineno,
                segment_uri=line,
            )
            continue

        duration, title = pending_inf
        byte_range: Optional[ByteRange] = None
        if pending_byterange is not None:
            length, offset = pending_byterange
            if offset is None:
                prev = segments[-1] if segments else None
                if (
                    prev is not None
                    and prev.byte_range is not None
                    and prev.uri == line
                ):
                    # 隐式偏移：继承同一资源上一子范围的 end
                    offset = prev.byte_range.end
                else:
                    log.error(
                        FailureCategory.BYTERANGE_UNRESOLVABLE.value,
                        "EXT-X-BYTERANGE without offset and no inheritable "
                        "previous sub-range of the same resource",
                        line=lineno,
                        segment_uri=line,
                        prev_uri=prev.uri if prev else None,
                    )
                    pending_inf = None
                    pending_byterange = None
                    pending_discontinuity = False
                    pending_gap = False
                    continue
            byte_range = ByteRange(offset=offset, length=length)

        segments.append(
            Segment(
                media_sequence=media_sequence + len(segments),
                discontinuity_sequence=discontinuity_sequence + disc_offset,
                uri=line,
                duration=duration,
                title=title,
                byte_range=byte_range,
                discontinuity_before=pending_discontinuity,
                gap=pending_gap,
                map_uri=map_uri,
                map_byte_range=map_byterange,
            )
        )
        pending_inf = None
        pending_byterange = None
        pending_discontinuity = False
        pending_gap = False

    if pending_inf is not None:
        log.error(
            FailureCategory.PARSE_ERROR.value,
            "trailing EXTINF without segment URI",
        )
    if target_duration is None:
        log.error(FailureCategory.PARSE_ERROR.value, "missing EXT-X-TARGETDURATION")

    if log.has_errors:
        raise ParseFailure(log)

    snapshot = PlaylistSnapshot(
        name=name,
        media_sequence=media_sequence,
        discontinuity_sequence=discontinuity_sequence,
        target_duration=target_duration,
        endlist=endlist,
        segments=segments,
        playlist_type=playlist_type,
        source_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )
    log.info(
        "PARSED",
        f"parsed {len(segments)} segments",
        media_sequence=media_sequence,
        discontinuity_sequence=discontinuity_sequence,
        endlist=endlist,
    )
    return snapshot
