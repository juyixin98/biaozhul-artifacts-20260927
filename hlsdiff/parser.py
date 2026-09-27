"""M3U8 媒体播放列表解析。

范围限定:非加密分段媒体。
- EXT-X-KEY METHOD 非 NONE → ENCRYPTION_UNSUPPORTED,拒绝解析。
- EXT-X-BYTERANGE 隐式偏移(只有长度没有 @offset)在解析期继承:
  与前一分段同 URI 时 offset = 前一分段 offset+length,否则报错。
- 单例标签(MEDIA-SEQUENCE / TARGETDURATION / VERSION /
  DISCONTINUITY-SEQUENCE / PLAYLIST-TYPE)重复出现 → DUPLICATE_TAG。
- ENDLIST 之后出现任何内容 → CONTENT_AFTER_ENDLIST。
"""

from __future__ import annotations

from .errors import FailureCategory, PlaylistParseError
from .models import ByteRange, Playlist, Segment

_SINGLETON_TAGS = {
    "EXT-X-VERSION",
    "EXT-X-TARGETDURATION",
    "EXT-X-MEDIA-SEQUENCE",
    "EXT-X-DISCONTINUITY-SEQUENCE",
    "EXT-X-PLAYLIST-TYPE",
}


def _parse_int(value: str, tag: str, line_no: int) -> int:
    try:
        return int(value)
    except ValueError:
        raise PlaylistParseError(
            FailureCategory.BAD_TAG_VALUE, f"{tag} 需要整数,得到 {value!r}", line_no
        ) from None


def _parse_float(value: str, tag: str, line_no: int) -> float:
    try:
        return float(value)
    except ValueError:
        raise PlaylistParseError(
            FailureCategory.BAD_TAG_VALUE, f"{tag} 需要数值,得到 {value!r}", line_no
        ) from None


def _parse_byte_range(value: str, line_no: int) -> tuple[int, int | None]:
    """解析 'n[@o]',返回 (length, offset|None)。"""
    if "@" in value:
        length_s, offset_s = value.split("@", 1)
        offset: int | None = _parse_int(offset_s, "EXT-X-BYTERANGE", line_no)
    else:
        length_s, offset = value, None
    length = _parse_int(length_s, "EXT-X-BYTERANGE", line_no)
    if length <= 0 or (offset is not None and offset < 0):
        raise PlaylistParseError(
            FailureCategory.BAD_TAG_VALUE, f"EXT-X-BYTERANGE 非法: {value!r}", line_no
        )
    return length, offset


def parse_playlist(text: str) -> Playlist:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines or lines[0] != "#EXTM3U":
        raise PlaylistParseError(FailureCategory.MISSING_EXTM3U, "首行必须是 #EXTM3U", 1)

    version = 3
    target_duration = 0.0
    media_sequence = 0
    discontinuity_sequence = 0
    playlist_type: str | None = None
    endlist = False

    seen_singletons: set[str] = set()
    segments: list[Segment] = []
    pending_duration: float | None = None
    pending_title = ""
    pending_byte_range: tuple[int, int | None] | None = None
    pending_discontinuity = False
    current_discontinuity_sequence = discontinuity_sequence

    for line_no, line in enumerate(lines[1:], start=2):
        if endlist:
            raise PlaylistParseError(
                FailureCategory.CONTENT_AFTER_ENDLIST,
                "EXT-X-ENDLIST 之后不允许再有内容",
                line_no,
            )
        if not line.startswith("#"):
            # URI 行:必须有前置 EXTINF
            if pending_duration is None:
                raise PlaylistParseError(
                    FailureCategory.MISSING_EXTINF, f"URI {line!r} 前缺少 EXTINF", line_no
                )
            byte_range = _resolve_byte_range(pending_byte_range, segments, line, line_no)
            if pending_discontinuity:
                current_discontinuity_sequence += 1
            segments.append(
                Segment(
                    sequence=media_sequence + len(segments),
                    uri=line,
                    duration=pending_duration,
                    title=pending_title,
                    discontinuity=pending_discontinuity,
                    discontinuity_sequence=current_discontinuity_sequence,
                    byte_range=byte_range,
                )
            )
            pending_duration, pending_title = None, ""
            pending_byte_range = None
            pending_discontinuity = False
            continue

        tag_body = line[1:]
        tag, _, value = tag_body.partition(":")

        if tag in _SINGLETON_TAGS:
            if tag in seen_singletons:
                raise PlaylistParseError(
                    FailureCategory.DUPLICATE_TAG, f"标签 {tag} 重复出现", line_no
                )
            seen_singletons.add(tag)

        if tag == "EXT-X-VERSION":
            version = _parse_int(value, tag, line_no)
        elif tag == "EXT-X-TARGETDURATION":
            target_duration = _parse_float(value, tag, line_no)
        elif tag == "EXT-X-MEDIA-SEQUENCE":
            media_sequence = _parse_int(value, tag, line_no)
            if media_sequence < 0:
                raise PlaylistParseError(
                    FailureCategory.BAD_TAG_VALUE, "MEDIA-SEQUENCE 不能为负", line_no
                )
        elif tag == "EXT-X-DISCONTINUITY-SEQUENCE":
            discontinuity_sequence = _parse_int(value, tag, line_no)
            current_discontinuity_sequence = discontinuity_sequence
        elif tag == "EXT-X-PLAYLIST-TYPE":
            playlist_type = value
        elif tag == "EXTINF":
            dur_s, _, title = value.partition(",")
            pending_duration = _parse_float(dur_s, tag, line_no)
            pending_title = title
        elif tag == "EXT-X-BYTERANGE":
            pending_byte_range = _parse_byte_range(value, line_no)
        elif tag == "EXT-X-DISCONTINUITY":
            pending_discontinuity = True
        elif tag == "EXT-X-KEY":
            attrs = _parse_attributes(value)
            method = attrs.get("METHOD", "")
            if method and method != "NONE":
                raise PlaylistParseError(
                    FailureCategory.ENCRYPTION_UNSUPPORTED,
                    f"仅支持非加密媒体,EXT-X-KEY METHOD={method}",
                    line_no,
                )
        elif tag == "EXT-X-ENDLIST":
            endlist = True
        # 其余标签(EXT-X-MAP、EXT-X-PROGRAM-DATE-TIME 等)按非关键标签忽略

    if pending_duration is not None:
        raise PlaylistParseError(
            FailureCategory.DANGLING_EXTINF, "EXTINF 之后缺少对应 URI 行", len(lines)
        )

    return Playlist(
        version=version,
        target_duration=target_duration,
        media_sequence=media_sequence,
        discontinuity_sequence=discontinuity_sequence,
        playlist_type=playlist_type,
        endlist=endlist,
        segments=tuple(segments),
    )


def _resolve_byte_range(
    pending: tuple[int, int | None] | None,
    segments: list[Segment],
    uri: str,
    line_no: int,
) -> ByteRange | None:
    """把 (length, offset|None) 解析为绝对 ByteRange,处理隐式偏移继承。"""
    if pending is None:
        return None
    length, offset = pending
    if offset is not None:
        return ByteRange(length=length, offset=offset)
    # 隐式偏移:必须与前一分段同 URI,从其末尾继承
    prev = segments[-1] if segments else None
    if prev is not None and prev.byte_range is not None and prev.uri == uri:
        return ByteRange(length=length, offset=prev.byte_range.end)
    raise PlaylistParseError(
        FailureCategory.BYTE_RANGE_OFFSET_UNRESOLVABLE,
        f"URI {uri!r} 的 EXT-X-BYTERANGE 缺少显式偏移,且无法从前一分段继承",
        line_no,
    )


def _parse_attributes(value: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for item in value.split(","):
        key, _, v = item.partition("=")
        attrs[key.strip()] = v.strip().strip('"')
    return attrs
