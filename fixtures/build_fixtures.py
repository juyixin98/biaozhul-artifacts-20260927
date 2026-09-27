#!/usr/bin/env python3
"""合成 MP4 夹具构建器。

生成 fixtures/generated/ 下的测试文件与 payloads.json（逐样本载荷 sha1，
供校验层独立复核解析器给出的字节范围）。

夹具清单：
- bframes.mp4      B 帧重排（ctts v1，含负偏移），2 样本/chunk
- empty_edit.mp4   前置空编辑（media_time=-1）+ 普通编辑
- trim_edit.mp4    裁剪编辑：段起点包含、段终点排除（边界规则）
- multi_edit.mp4   两段普通编辑，中间样本不被任何段覆盖
- multitrack.mp4   视频 90000 + 音频 48000 双轨，电影 1000（有理数换算）
- bad_length.mp4   盒声明长度越界（负例）
- fragmented.mp4   含 moof 的分片布局（负例）
- encrypted.mp4    stsd 样本条目为 encv（负例）

参考时间表（fixtures/references/*.json）是手工编写的，不由本构建器生成。

用法：python fixtures/build_fixtures.py
"""

from __future__ import annotations

import hashlib
import json
import struct
import sys
from pathlib import Path

GENERATED_DIR = Path(__file__).resolve().parent / "generated"

MATRIX = struct.pack(">9i", 0x00010000, 0, 0, 0, 0x00010000, 0, 0, 0, 0x40000000)
LANG_UND = 0x55C4  # 'und'


# ---------------------------------------------------------------- 盒写出器


def box(typ: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), typ) + payload


def full_box(typ: bytes, version: int, flags: int, payload: bytes) -> bytes:
    return box(typ, bytes([version]) + flags.to_bytes(3, "big") + payload)


def ftyp() -> bytes:
    return box(b"ftyp", b"isom" + struct.pack(">I", 0) + b"isomiso2avc1mp41")


def mvhd(timescale: int, duration: int, next_track_id: int) -> bytes:
    p = struct.pack(">IIII", 0, 0, timescale, duration)
    p += struct.pack(">I", 0x00010000) + struct.pack(">H", 0x0100) + b"\0" * 10
    p += MATRIX + b"\0" * 24 + struct.pack(">I", next_track_id)
    return full_box(b"mvhd", 0, 0, p)


def tkhd(track_id: int, duration: int, width: int, height: int, volume: int) -> bytes:
    p = struct.pack(">II", 0, 0)
    p += struct.pack(">I", track_id) + b"\0" * 4 + struct.pack(">I", duration)
    p += b"\0" * 8
    p += struct.pack(">hhhh", 0, 0, volume, 0)
    p += MATRIX
    p += struct.pack(">II", width << 16, height << 16)
    return full_box(b"tkhd", 0, 0x7, p)


def mdhd(timescale: int, duration: int) -> bytes:
    return full_box(b"mdhd", 0, 0, struct.pack(">IIIIHH", 0, 0, timescale, duration, LANG_UND, 0))


def hdlr(kind: bytes) -> bytes:
    name = b"VideoHandler\0" if kind == b"vide" else b"SoundHandler\0"
    return full_box(b"hdlr", 0, 0, b"\0" * 4 + kind + b"\0" * 12 + name)


def video_sample_entry(width: int, height: int, entry_type: bytes = b"avc1") -> bytes:
    p = b"\0" * 6 + struct.pack(">H", 1)  # reserved + data_reference_index
    p += b"\0" * 16  # pre_defined + reserved
    p += struct.pack(">HH", width, height)
    p += struct.pack(">II", 0x00480000, 0x00480000)  # 72dpi
    p += b"\0" * 4 + struct.pack(">H", 1) + b"\0" * 32
    p += struct.pack(">Hh", 0x18, -1)  # depth, pre_defined
    return box(entry_type, p)


def audio_sample_entry(sample_rate: int, entry_type: bytes = b"mp4a") -> bytes:
    p = b"\0" * 6 + struct.pack(">H", 1)
    p += b"\0" * 8
    p += struct.pack(">HHHHI", 2, 16, 0, 0, sample_rate << 16)
    return box(entry_type, p)


def stsd(entry: bytes) -> bytes:
    return full_box(b"stsd", 0, 0, struct.pack(">I", 1) + entry)


def stts(entries: list[tuple[int, int]]) -> bytes:
    p = struct.pack(">I", len(entries))
    for count, delta in entries:
        p += struct.pack(">II", count, delta)
    return full_box(b"stts", 0, 0, p)


def ctts_v1(entries: list[tuple[int, int]]) -> bytes:
    p = struct.pack(">I", len(entries))
    for count, offset in entries:
        p += struct.pack(">Ii", count, offset)  # 有符号
    return full_box(b"ctts", 1, 0, p)


def stss(numbers: list[int]) -> bytes:
    p = struct.pack(">I", len(numbers))
    for n in numbers:
        p += struct.pack(">I", n)
    return full_box(b"stss", 0, 0, p)


def stsc(entries: list[tuple[int, int, int]]) -> bytes:
    p = struct.pack(">I", len(entries))
    for first_chunk, spc, desc in entries:
        p += struct.pack(">III", first_chunk, spc, desc)
    return full_box(b"stsc", 0, 0, p)


def stsz(sizes: list[int]) -> bytes:
    p = struct.pack(">II", 0, len(sizes))
    for s in sizes:
        p += struct.pack(">I", s)
    return full_box(b"stsz", 0, 0, p)


def stco(offsets: list[int]) -> bytes:
    p = struct.pack(">I", len(offsets))
    for o in offsets:
        p += struct.pack(">I", o)
    return full_box(b"stco", 0, 0, p)


def elst_v0(entries: list[tuple[int, int]]) -> bytes:
    """entries: [(segment_duration, media_time)]，rate 固定 1.0。"""

    p = struct.pack(">I", len(entries))
    for seg_dur, media_time in entries:
        p += struct.pack(">Ii", seg_dur, media_time)
        p += struct.pack(">hH", 1, 0)
    return full_box(b"elst", 0, 0, p)


def dinf() -> bytes:
    url = full_box(b"url ", 0, 1, b"")
    dref = full_box(b"dref", 0, 0, struct.pack(">I", 1) + url)
    return box(b"dinf", dref)


# ---------------------------------------------------------------- 轨道规格


class TrackSpec:
    def __init__(
        self,
        track_id: int,
        kind: str,  # 'video' | 'audio'
        media_timescale: int,
        sizes: list[int],
        stts_entries: list[tuple[int, int]],
        ctts_entries: list[tuple[int, int]] | None = None,
        stsc_entries: list[tuple[int, int, int]] | None = None,
        stss_numbers: list[int] | None = None,
        elst_entries: list[tuple[int, int]] | None = None,
        width: int = 640,
        height: int = 360,
        sample_rate: int = 48000,
        entry_type: bytes | None = None,
    ):
        self.track_id = track_id
        self.kind = kind
        self.media_timescale = media_timescale
        self.sizes = sizes
        self.stts_entries = stts_entries
        self.ctts_entries = ctts_entries
        self.stsc_entries = stsc_entries or [(1, len(sizes), 1)]
        self.stss_numbers = stss_numbers
        self.elst_entries = elst_entries
        self.width = width
        self.height = height
        self.sample_rate = sample_rate
        self.entry_type = entry_type

    @property
    def media_duration(self) -> int:
        return sum(c * d for c, d in self.stts_entries)

    def chunk_layout(self) -> list[list[int]]:
        """按 stsc 规格把样本（0 基）分组到 chunk，返回每组样本下标。"""

        groups: list[list[int]] = []
        idx = 0
        chunk_no = 1
        while idx < len(self.sizes):
            # 取 first_chunk<=chunk_no 的最后一条（升序列表的适用项）
            applicable = [e for e in self.stsc_entries if e[0] <= chunk_no]
            spc = applicable[-1][1]
            groups.append(list(range(idx, min(idx + spc, len(self.sizes)))))
            idx += spc
            chunk_no += 1
        return groups


def sample_payload(track_id: int, index: int, size: int) -> bytes:
    """确定性的样本载荷：模式串重复/截断到 size。

    校验层用它独立验证解析器给出的字节范围。
    """

    pattern = b"T%dS%04d|" % (track_id, index)
    return (pattern * (size // len(pattern) + 1))[:size]


def build_track_box(spec: TrackSpec, chunk_offsets: list[int]) -> bytes:
    if spec.kind == "video":
        entry = video_sample_entry(
            spec.width, spec.height, spec.entry_type or b"avc1"
        )
        media_header = full_box(b"vmhd", 0, 1, struct.pack(">H6s", 0, b"\0" * 6))
        handler = b"vide"
        volume = 0
    else:
        entry = audio_sample_entry(spec.sample_rate, spec.entry_type or b"mp4a")
        media_header = full_box(b"smhd", 0, 0, struct.pack(">Hh", 0, 0))
        handler = b"soun"
        volume = 0x0100

    stbl_children = stsd(entry) + stts(spec.stts_entries)
    if spec.ctts_entries is not None:
        stbl_children += ctts_v1(spec.ctts_entries)
    if spec.stss_numbers is not None:
        stbl_children += stss(spec.stss_numbers)
    stbl_children += stsc(spec.stsc_entries) + stsz(spec.sizes) + stco(chunk_offsets)
    stbl = box(b"stbl", stbl_children)

    minf = box(b"minf", media_header + dinf() + stbl)
    mdia = box(
        b"mdia",
        mdhd(spec.media_timescale, spec.media_duration) + hdlr(handler) + minf,
    )

    tkhd_duration = (
        sum(d for d, _ in spec.elst_entries) if spec.elst_entries else spec.media_duration
    )
    head = tkhd(
        spec.track_id,
        tkhd_duration,
        spec.width if spec.kind == "video" else 0,
        spec.height if spec.kind == "video" else 0,
        volume,
    )
    edts = b""
    if spec.elst_entries is not None:
        edts = box(b"edts", elst_v0(spec.elst_entries))
    return box(b"trak", head + edts + mdia)


def build_file(movie_timescale: int, movie_duration: int, specs: list[TrackSpec]) -> tuple[bytes, dict]:
    """两遍构建：先以 0 偏移量出 moov 长度，再回填真实 stco。

    返回 (文件字节, {track_id: {sample_index: payload_sha1}})。
    """

    ft = ftyp()

    # 每轨 chunk 分组（两遍一致）
    layouts = {s.track_id: s.chunk_layout() for s in specs}

    def moov_with(offsets_by_track: dict[int, list[int]]) -> bytes:
        traks = b"".join(
            build_track_box(s, offsets_by_track[s.track_id]) for s in specs
        )
        return box(
            b"moov", mvhd(movie_timescale, movie_duration, len(specs) + 1) + traks
        )

    zero_offsets = {
        s.track_id: [0] * len(layouts[s.track_id]) for s in specs
    }
    moov0 = moov_with(zero_offsets)
    mdat_start = len(ft) + len(moov0) + 8  # mdat 盒头 8 字节

    # 真实 chunk 偏移 + mdat 载荷
    offsets_by_track: dict[int, list[int]] = {}
    payloads: dict[int, dict[int, str]] = {}
    mdat_payload = b""
    cursor = mdat_start
    for s in specs:
        offsets: list[int] = []
        payloads[s.track_id] = {}
        for group in layouts[s.track_id]:
            offsets.append(cursor)
            for sample_index in group:
                payload = sample_payload(s.track_id, sample_index, s.sizes[sample_index])
                mdat_payload += payload
                cursor += len(payload)
                payloads[s.track_id][sample_index] = hashlib.sha1(payload).hexdigest()
        offsets_by_track[s.track_id] = offsets

    moov1 = moov_with(offsets_by_track)
    assert len(moov1) == len(moov0), "两遍 moov 长度必须一致（stco 定长）"
    data = ft + moov1 + box(b"mdat", mdat_payload)
    return data, payloads


# ---------------------------------------------------------------- 夹具定义


def fixture_bframes() -> tuple[bytes, dict, int, int]:
    """B 帧重排：解码序 I P B B P B，ctts v1 含负偏移。"""

    spec = TrackSpec(
        track_id=1,
        kind="video",
        media_timescale=1000,
        sizes=[500, 300, 120, 130, 400, 110],
        stts_entries=[(6, 100)],
        ctts_entries=[(1, 0), (1, 200), (2, -100), (1, 100), (1, -100)],
        stsc_entries=[(1, 2, 1)],
        stss_numbers=[1, 5],
        elst_entries=[(600, 0)],
    )
    data, payloads = build_file(1000, 600, [spec])
    return data, payloads, 1000, 600


def fixture_empty_edit() -> tuple[bytes, dict, int, int]:
    spec = TrackSpec(
        track_id=1,
        kind="video",
        media_timescale=1000,
        sizes=[200, 210, 220, 230],
        stts_entries=[(4, 250)],
        stsc_entries=[(1, 4, 1)],
        elst_entries=[(500, -1), (1000, 0)],
    )
    data, payloads = build_file(1000, 1500, [spec])
    return data, payloads, 1000, 1500


def fixture_trim_edit() -> tuple[bytes, dict, int, int]:
    spec = TrackSpec(
        track_id=1,
        kind="video",
        media_timescale=1000,
        sizes=[200, 210, 220, 230],
        stts_entries=[(4, 250)],
        stsc_entries=[(1, 4, 1)],
        elst_entries=[(500, 250)],
    )
    data, payloads = build_file(1000, 500, [spec])
    return data, payloads, 1000, 500


def fixture_multi_edit() -> tuple[bytes, dict, int, int]:
    spec = TrackSpec(
        track_id=1,
        kind="video",
        media_timescale=1000,
        sizes=[200, 210, 220, 230],
        stts_entries=[(4, 250)],
        stsc_entries=[(1, 4, 1)],
        elst_entries=[(250, 0), (250, 500)],
    )
    data, payloads = build_file(1000, 500, [spec])
    return data, payloads, 1000, 500


def fixture_multitrack() -> tuple[bytes, dict, int, int]:
    video = TrackSpec(
        track_id=1,
        kind="video",
        media_timescale=90000,
        sizes=[600, 300, 320],
        stts_entries=[(3, 3000)],
        stsc_entries=[(1, 3, 1)],
        elst_entries=[(100, 0)],
    )
    audio = TrackSpec(
        track_id=2,
        kind="audio",
        media_timescale=48000,
        sizes=[180, 180, 180, 180, 180],
        stts_entries=[(5, 960)],
        stsc_entries=[(1, 5, 1)],
        elst_entries=[(100, 0)],
    )
    data, payloads = build_file(1000, 100, [video, audio])
    return data, payloads, 1000, 100


def main() -> int:
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    all_payloads: dict[str, dict] = {}

    builders = {
        "bframes.mp4": fixture_bframes,
        "empty_edit.mp4": fixture_empty_edit,
        "trim_edit.mp4": fixture_trim_edit,
        "multi_edit.mp4": fixture_multi_edit,
        "multitrack.mp4": fixture_multitrack,
    }
    for name, builder in builders.items():
        data, payloads, _, _ = builder()
        path = GENERATED_DIR / name
        path.write_bytes(data)
        all_payloads[name] = {
            str(tid): {str(idx): sha for idx, sha in samples.items()}
            for tid, samples in payloads.items()
        }
        print(f"{name}: {len(data)} 字节 sha256={hashlib.sha256(data).hexdigest()[:16]}...")

    # ---- 负例夹具 ----
    # 1) 盒长度越界：顶层盒声明长度远超文件实际长度
    bad = ftyp() + struct.pack(">I4s", 0x0FFFFFF0, b"moov") + b"\0" * 16
    (GENERATED_DIR / "bad_length.mp4").write_bytes(bad)
    print(f"bad_length.mp4: {len(bad)} 字节（moov 声明 {0x0FFFFFF0}）")

    # 2) 分片布局：ftyp + moof(mfhd)
    mfhd = full_box(b"mfhd", 0, 0, struct.pack(">I", 1))
    frag = ftyp() + box(b"moof", mfhd)
    (GENERATED_DIR / "fragmented.mp4").write_bytes(frag)
    print(f"fragmented.mp4: {len(frag)} 字节（含 moof）")

    # 3) 加密样本条目：encv
    enc_spec = TrackSpec(
        track_id=1,
        kind="video",
        media_timescale=1000,
        sizes=[100],
        stts_entries=[(1, 100)],
        entry_type=b"encv",
    )
    enc_data, _ = build_file(1000, 100, [enc_spec])
    (GENERATED_DIR / "encrypted.mp4").write_bytes(enc_data)
    print(f"encrypted.mp4: {len(enc_data)} 字节（stsd 条目 encv）")

    (GENERATED_DIR / "payloads.json").write_text(
        json.dumps(all_payloads, indent=1, ensure_ascii=False)
    )
    print(f"payloads.json: {sum(len(v) for v in all_payloads.values())} 轨的样本 sha1")
    return 0


if __name__ == "__main__":
    sys.exit(main())
