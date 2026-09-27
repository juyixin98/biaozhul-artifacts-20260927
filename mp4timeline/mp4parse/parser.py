"""整文件解析：把字节流解析为 Movie/Track 模型。

支持范围（受限）：
- 非分片 MP4（顶层出现 moof/mfra，或 moov 内出现 mvex → 拒绝）；
- 非加密内容（stsd 样本条目为 encv/enca/enct，或出现 sinf/pssh 等 → 拒绝）；
- stco 或 co64 chunk 偏移；
- mdhd/tkhd/elst 的 version 0 与 1；
- ctts version 0（无符号）与 1（有符号）。
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass, field

from ..errors import (
    BoxLengthError,
    MissingBoxError,
    SampleTableError,
    UnsupportedLayoutError,
)
from . import sample_table as st
from .boxes import (
    ENCRYPTED_SAMPLE_ENTRY_TYPES,
    ENCRYPTION_BOX_TYPES,
    FRAGMENT_SIGNAL_MOOV,
    FRAGMENT_SIGNAL_TOP,
    Box,
    find_child,
    find_path,
    iter_boxes,
)
from .editlist import Edit, parse_elst


@dataclass(frozen=True)
class Track:
    track_id: int
    handler: str  # 'vide' / 'soun' / ...
    media_timescale: int
    media_duration: int  # media timescale 单位
    samples: tuple[st.Sample, ...]
    edits: tuple[Edit, ...]  # 无 edts/elst 时为空，内核按恒等编辑处理


@dataclass(frozen=True)
class Movie:
    movie_timescale: int
    movie_duration: int  # movie timescale 单位
    tracks: tuple[Track, ...]
    source: str
    sha256: str
    file_bytes: int


def _parse_mvhd(data: bytes, box: Box) -> tuple[int, int]:
    version = data[box.payload_start]
    p = box.payload_start + 4
    if version == 1:
        timescale, duration = struct.unpack_from(">IQ", data, p + 16)
    elif version == 0:
        timescale, duration = struct.unpack_from(">II", data, p + 8)
    else:
        raise UnsupportedLayoutError(f"mvhd 不支持 version={version}")
    if timescale == 0:
        raise SampleTableError("mvhd timescale 为 0")
    return timescale, duration


def _parse_mdhd(data: bytes, box: Box) -> tuple[int, int]:
    version = data[box.payload_start]
    p = box.payload_start + 4
    if version == 1:
        timescale, duration = struct.unpack_from(">IQ", data, p + 16)
    elif version == 0:
        timescale, duration = struct.unpack_from(">II", data, p + 8)
    else:
        raise UnsupportedLayoutError(f"mdhd 不支持 version={version}")
    if timescale == 0:
        raise SampleTableError("mdhd timescale 为 0")
    return timescale, duration


def _parse_tkhd(data: bytes, box: Box) -> int:
    version = data[box.payload_start]
    p = box.payload_start + 4
    if version == 1:
        (track_id,) = struct.unpack_from(">I", data, p + 16)
    elif version == 0:
        (track_id,) = struct.unpack_from(">I", data, p + 8)
    else:
        raise UnsupportedLayoutError(f"tkhd 不支持 version={version}")
    return track_id


def _check_not_encrypted(data: bytes, stsd: Box) -> None:
    """stsd 第一个样本条目类型若为加密类型，或 stbl 内含加密盒，则拒绝。"""

    p = stsd.payload_start + 4  # full-box 头
    (entry_count,) = struct.unpack_from(">I", data, p)
    p += 4
    if entry_count == 0:
        raise SampleTableError("stsd 没有任何样本条目")
    entry_type = data[p + 4 : p + 8].decode("latin1")
    if entry_type in ENCRYPTED_SAMPLE_ENTRY_TYPES:
        raise UnsupportedLayoutError(
            f"加密样本条目 '{entry_type}' 不在支持范围（仅限非加密 MP4）"
        )


def _parse_track(data: bytes, trak: Box) -> Track:
    tkhd = find_child(data, trak, "tkhd")
    if tkhd is None:
        raise MissingBoxError("trak 缺少 tkhd")
    track_id = _parse_tkhd(data, tkhd)

    mdia = find_child(data, trak, "mdia")
    if mdia is None:
        raise MissingBoxError("trak 缺少 mdia")
    mdhd = find_child(data, mdia, "mdhd")
    hdlr = find_child(data, mdia, "hdlr")
    if mdhd is None or hdlr is None:
        raise MissingBoxError("mdia 缺少 mdhd 或 hdlr")
    media_timescale, media_duration = _parse_mdhd(data, mdhd)
    handler = data[hdlr.payload_start + 8 : hdlr.payload_start + 12].decode("latin1")

    stbl = find_path(data, mdia, "minf", "stbl")
    if stbl is None:
        raise MissingBoxError("缺少 minf/stbl")

    stsd = find_child(data, stbl, "stsd")
    if stsd is None:
        raise MissingBoxError("stbl 缺少 stsd")
    _check_not_encrypted(data, stsd)

    stts_box = find_child(data, stbl, "stts")
    stsc_box = find_child(data, stbl, "stsc")
    stsz_box = find_child(data, stbl, "stsz")
    if stts_box is None or stsc_box is None or stsz_box is None:
        raise MissingBoxError("stbl 缺少 stts/stsc/stsz 之一")

    ctts_box = find_child(data, stbl, "ctts")
    stss_box = find_child(data, stbl, "stss")
    stco_box = find_child(data, stbl, "stco")
    co64_box = find_child(data, stbl, "co64")

    samples = st.build_samples(
        stts=st.parse_stts(data, stts_box),
        ctts=st.parse_ctts(data, ctts_box) if ctts_box is not None else None,
        stsc=st.parse_stsc(data, stsc_box),
        sizes=st.parse_stsz(data, stsz_box),
        chunk_offsets=st.parse_chunk_offsets(data, stco_box, co64_box),
        sync_numbers=st.parse_stss(data, stss_box) if stss_box is not None else None,
    )

    # 样本字节范围不得越出文件。
    for sample in samples:
        if sample.byte_offset < 0 or sample.byte_offset + sample.size > len(data):
            raise BoxLengthError(
                f"样本 {sample.index} 字节范围 "
                f"[{sample.byte_offset}, {sample.byte_offset + sample.size}) "
                f"超出文件长度 {len(data)}"
            )

    edits: tuple[Edit, ...] = ()
    elst_box = find_path(data, trak, "edts", "elst")
    if elst_box is not None:
        edits = tuple(parse_elst(data, elst_box))

    return Track(
        track_id=track_id,
        handler=handler,
        media_timescale=media_timescale,
        media_duration=media_duration,
        samples=tuple(samples),
        edits=edits,
    )


def parse_movie(data: bytes, source: str = "<bytes>") -> Movie:
    """解析完整 MP4 字节流为 :class:`Movie`。"""

    top_boxes = list(iter_boxes(data, 0, len(data)))
    top_types = {b.type for b in top_boxes}
    if top_types & FRAGMENT_SIGNAL_TOP:
        raise UnsupportedLayoutError(
            f"检测到分片布局信号 {sorted(top_types & FRAGMENT_SIGNAL_TOP)}，"
            "本服务不支持分片 MP4"
        )

    moov = next((b for b in top_boxes if b.type == "moov"), None)
    if moov is None:
        raise MissingBoxError("缺少 moov 盒")

    moov_children = list(iter_boxes(data, moov.payload_start, moov.payload_end))
    moov_types = {b.type for b in moov_children}
    if moov_types & FRAGMENT_SIGNAL_MOOV:
        raise UnsupportedLayoutError(
            f"moov 内含分片信号 {sorted(moov_types & FRAGMENT_SIGNAL_MOOV)}，"
            "本服务不支持分片 MP4"
        )
    if moov_types & ENCRYPTION_BOX_TYPES:
        raise UnsupportedLayoutError("moov 内含加密相关盒，仅限非加密 MP4")

    mvhd = next((b for b in moov_children if b.type == "mvhd"), None)
    if mvhd is None:
        raise MissingBoxError("moov 缺少 mvhd")
    movie_timescale, movie_duration = _parse_mvhd(data, mvhd)

    tracks = tuple(
        _parse_track(data, b) for b in moov_children if b.type == "trak"
    )
    if not tracks:
        raise MissingBoxError("moov 内没有任何 trak")

    return Movie(
        movie_timescale=movie_timescale,
        movie_duration=movie_duration,
        tracks=tracks,
        source=source,
        sha256=hashlib.sha256(data).hexdigest(),
        file_bytes=len(data),
    )
