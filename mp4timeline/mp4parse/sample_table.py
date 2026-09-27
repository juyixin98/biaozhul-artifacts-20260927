"""stbl 样本表解析与样本记录构建。

解码顺序（DTS）由 stts 累计得到；呈现顺序偏移（CTO）由 ctts 给出，
ctts version 1 的偏移是有符号整数，符号必须保留：

    PTS = DTS + CTO

样本字节范围由 stsc（样本→chunk）、stco/co64（chunk 偏移）、
stsz（样本大小）三者联合推出。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

import numpy as np

from ..errors import SampleTableError
from .boxes import Box

# 样本编号 / chunk 编号在 MP4 中均从 1 开始。


@dataclass(frozen=True)
class Sample:
    """一个解码样本的完整记录。

    时间字段单位均为该轨的 media timescale。
    """

    index: int  # 0 基，即文件中的样本号 - 1
    dts: int  # 解码时间（解码顺序即样本表顺序）
    cto: int  # 组合偏移，有符号，version 1 可为负
    duration: int
    byte_offset: int  # 在文件中的绝对字节偏移
    size: int
    is_sync: bool

    @property
    def pts(self) -> int:
        """呈现时间 = DTS + 有符号 CTO。"""
        return self.dts + self.cto


def _full_header(box: Box, data: bytes) -> tuple[int, int, int]:
    """返回 (version, flags, full-box 载荷起点)。"""

    version = data[box.payload_start]
    flags = int.from_bytes(data[box.payload_start + 1 : box.payload_start + 4], "big")
    return version, flags, box.payload_start + 4


def parse_stts(data: bytes, box: Box) -> list[tuple[int, int]]:
    """stts：[(sample_count, sample_delta), ...]，run-length 编码的 DTS 增量。"""

    version, _, p = _full_header(box, data)
    if version != 0:
        raise SampleTableError(f"stts 不支持 version={version}")
    entry_count = struct.unpack_from(">I", data, p)[0]
    entries: list[tuple[int, int]] = []
    pos = p + 4
    for _ in range(entry_count):
        count, delta = struct.unpack_from(">II", data, pos)
        entries.append((count, delta))
        pos += 8
    expected_end = box.payload_end
    if pos != expected_end:
        raise SampleTableError(
            f"stts 条目字节数不一致：解析到 {pos}，盒结束于 {expected_end}"
        )
    return entries


def parse_ctts(data: bytes, box: Box) -> list[tuple[int, int]]:
    """ctts：[(sample_count, composition_offset), ...]。

    version 0 偏移为 uint32；version 1 为 int32（有符号，可负）。
    这里统一按盒版本解码为有符号 Python int，符号保留。
    """

    version, _, p = _full_header(box, data)
    if version not in (0, 1):
        raise SampleTableError(f"ctts 不支持 version={version}")
    entry_count = struct.unpack_from(">I", data, p)[0]
    entries: list[tuple[int, int]] = []
    pos = p + 4
    for _ in range(entry_count):
        count = struct.unpack_from(">I", data, pos)[0]
        if version == 1:
            offset = struct.unpack_from(">i", data, pos + 4)[0]  # 有符号
        else:
            offset = struct.unpack_from(">I", data, pos + 4)[0]  # 无符号
        entries.append((count, offset))
        pos += 8
    if pos != box.payload_end:
        raise SampleTableError(
            f"ctts 条目字节数不一致：解析到 {pos}，盒结束于 {box.payload_end}"
        )
    return entries


def parse_stsc(data: bytes, box: Box) -> list[tuple[int, int, int]]:
    """stsc：[(first_chunk, samples_per_chunk, sample_description_index), ...]。"""

    version, _, p = _full_header(box, data)
    if version != 0:
        raise SampleTableError(f"stsc 不支持 version={version}")
    entry_count = struct.unpack_from(">I", data, p)[0]
    entries: list[tuple[int, int, int]] = []
    pos = p + 4
    for _ in range(entry_count):
        first_chunk, spc, desc_idx = struct.unpack_from(">III", data, pos)
        entries.append((first_chunk, spc, desc_idx))
        pos += 12
    if pos != box.payload_end:
        raise SampleTableError(
            f"stsc 条目字节数不一致：解析到 {pos}，盒结束于 {box.payload_end}"
        )
    if entries != sorted(entries, key=lambda e: e[0]):
        raise SampleTableError("stsc first_chunk 必须严格升序")
    return entries


def parse_stsz(data: bytes, box: Box) -> list[int]:
    """stsz：返回逐样本大小列表。sample_size!=0 表示所有样本同大小。"""

    version, _, p = _full_header(box, data)
    if version != 0:
        raise SampleTableError(f"stsz 不支持 version={version}")
    uniform_size, sample_count = struct.unpack_from(">II", data, p)
    pos = p + 8
    if uniform_size:
        sizes = [uniform_size] * sample_count
    else:
        sizes = list(struct.unpack_from(f">{sample_count}I", data, pos))
        pos += 4 * sample_count
    if pos != box.payload_end:
        raise SampleTableError(
            f"stsz 条目字节数不一致：解析到 {pos}，盒结束于 {box.payload_end}"
        )
    return sizes


def parse_chunk_offsets(data: bytes, stco: Box | None, co64: Box | None) -> list[int]:
    """stco（32 位）或 co64（64 位）chunk 偏移，二者必须恰好存在其一。"""

    if stco is not None and co64 is not None:
        raise SampleTableError("stco 与 co64 不能同时存在")
    box = stco or co64
    if box is None:
        raise SampleTableError("缺少 stco / co64（chunk 偏移表）")
    version, _, p = _full_header(box, data)
    if version != 0:
        raise SampleTableError(f"{box.type} 不支持 version={version}")
    entry_count = struct.unpack_from(">I", data, p)[0]
    pos = p + 4
    if box.type == "stco":
        offsets = list(struct.unpack_from(f">{entry_count}I", data, pos))
        pos += 4 * entry_count
    else:
        offsets = list(struct.unpack_from(f">{entry_count}Q", data, pos))
        pos += 8 * entry_count
    if pos != box.payload_end:
        raise SampleTableError(
            f"{box.type} 条目字节数不一致：解析到 {pos}，盒结束于 {box.payload_end}"
        )
    return offsets


def parse_stss(data: bytes, box: Box) -> set[int]:
    """stss：同步样本（关键帧）的 1 基样本号集合。无 stss 时全部视为同步。"""

    version, _, p = _full_header(box, data)
    if version != 0:
        raise SampleTableError(f"stss 不支持 version={version}")
    entry_count = struct.unpack_from(">I", data, p)[0]
    numbers = set(struct.unpack_from(f">{entry_count}I", data, p + 4))
    return numbers


def _expand_runs(
    entries: list[tuple[int, int]], name: str, expected: int | None = None
) -> np.ndarray:
    """把 (count, value) run-length 表展开为逐样本的 int64 数组。"""

    if not entries:
        raise SampleTableError(f"{name} 为空")
    counts = np.asarray([c for c, _ in entries], dtype=np.int64)
    values = np.asarray([v for _, v in entries], dtype=np.int64)
    if np.any(counts <= 0):
        raise SampleTableError(f"{name} 中存在 count<=0 的条目")
    total = int(counts.sum())
    if expected is not None and total != expected:
        raise SampleTableError(
            f"{name} 展开后样本数 {total} 与 stsz 样本数 {expected} 不一致"
        )
    return np.repeat(values, counts)


def _chunk_sample_counts(stsc: list[tuple[int, int, int]], n_chunks: int) -> list[int]:
    """对每个 1 基 chunk 求其样本数（取 first_chunk<=chunk 序号的最后一条 stsc）。"""

    if not stsc:
        raise SampleTableError("stsc 为空")
    first_chunks = [e[0] for e in stsc]
    if first_chunks[0] != 1:
        raise SampleTableError(f"stsc 第一条 first_chunk 必须为 1，实际 {first_chunks[0]}")
    counts: list[int] = []
    for chunk_no in range(1, n_chunks + 1):
        applicable = None
        for entry in stsc:
            if entry[0] <= chunk_no:
                applicable = entry
            else:
                break
        assert applicable is not None
        counts.append(applicable[1])
    return counts


def build_samples(
    stts: list[tuple[int, int]],
    ctts: list[tuple[int, int]] | None,
    stsc: list[tuple[int, int, int]],
    sizes: list[int],
    chunk_offsets: list[int],
    sync_numbers: set[int] | None,
) -> list[Sample]:
    """联合 stts/ctts/stsc/stsz/stco(stco) 构建逐样本记录。

    校验样本数一致、chunk 分组一致、字节范围不越出（这里只做表内一致性，
    文件边界在 parser 层用实际文件大小复核）。
    """

    n = len(sizes)
    if n == 0:
        raise SampleTableError("stsz 中没有任何样本")

    durations = _expand_runs(stts, "stts", expected=n)
    if ctts is None:
        offsets_cts = np.zeros(n, dtype=np.int64)
    else:
        offsets_cts = _expand_runs(ctts, "ctts", expected=n)

    # DTS：首个样本为 0，之后累加前一个样本的持续时间。
    dts = np.empty(n, dtype=np.int64)
    dts[0] = 0
    np.cumsum(durations[:-1], out=dts[1:])

    n_chunks = len(chunk_offsets)
    per_chunk = _chunk_sample_counts(stsc, n_chunks)
    if sum(per_chunk) != n:
        raise SampleTableError(
            f"stsc 分组样本总数 {sum(per_chunk)} 与 stsz 样本数 {n} 不一致"
        )

    all_sync = sync_numbers is None
    samples: list[Sample] = []
    sample_no = 0  # 0 基
    for chunk_idx, chunk_offset in enumerate(chunk_offsets):
        within = 0
        for _ in range(per_chunk[chunk_idx]):
            size = sizes[sample_no]
            samples.append(
                Sample(
                    index=sample_no,
                    dts=int(dts[sample_no]),
                    cto=int(offsets_cts[sample_no]),
                    duration=int(durations[sample_no]),
                    byte_offset=int(chunk_offset) + within,
                    size=int(size),
                    is_sync=all_sync or ((sample_no + 1) in sync_numbers),
                )
            )
            within += size
            sample_no += 1
    return samples
