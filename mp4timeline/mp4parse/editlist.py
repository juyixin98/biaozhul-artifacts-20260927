"""elst（edit list）解析。

每条 edit 定义媒体时间轴到电影时间轴的一段映射：

- ``segment_duration``：该段在电影时间轴上的时长（movie timescale 单位）；
- ``media_time``：该段起始的媒体时间（media timescale 单位）；
  ``media_time == -1`` 表示空编辑（空档）：电影时间轴前进
  ``segment_duration``，期间不呈现任何媒体样本；
- ``media_rate``：本服务仅支持 1.0（前向常速），其余明确拒绝。

version 0 用 32 位字段，version 1 用 64 位字段。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from ..errors import EditListError
from .boxes import Box

EMPTY_EDIT_MEDIA_TIME = -1


@dataclass(frozen=True)
class Edit:
    segment_duration: int  # movie timescale 单位
    media_time: int  # media timescale 单位；-1 表示空编辑
    media_rate_integer: int
    media_rate_fraction: int

    @property
    def is_empty(self) -> bool:
        return self.media_time == EMPTY_EDIT_MEDIA_TIME

    @property
    def rate(self) -> float:
        return self.media_rate_integer + self.media_rate_fraction / 65536.0


def parse_elst(data: bytes, box: Box) -> list[Edit]:
    version = data[box.payload_start]
    p = box.payload_start + 4
    entry_count = struct.unpack_from(">I", data, p)[0]
    p += 4
    edits: list[Edit] = []
    for _ in range(entry_count):
        if version == 1:
            seg_dur, media_time = struct.unpack_from(">Qq", data, p)
            p += 16
        elif version == 0:
            seg_dur, media_time = struct.unpack_from(">Ii", data, p)
            p += 8
        else:
            raise EditListError(f"elst 不支持 version={version}")
        rate_int, rate_frac = struct.unpack_from(">hH", data, p)
        p += 4
        edits.append(Edit(seg_dur, media_time, rate_int, rate_frac))
    if p != box.payload_end:
        raise EditListError(
            f"elst 条目字节数不一致：解析到 {p}，盒结束于 {box.payload_end}"
        )
    for edit in edits:
        if not edit.is_empty and edit.media_time < 0:
            raise EditListError(
                f"非空编辑的 media_time 不能为负：{edit.media_time}"
            )
        if edit.rate != 1.0:
            raise EditListError(
                f"仅支持 media_rate=1.0，实际 {edit.rate}（拒绝处理变速编辑）"
            )
    return edits
