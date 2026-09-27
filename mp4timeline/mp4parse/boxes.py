"""MP4 盒（box/atom）读取层。

只负责结构，不解释盒内容：
- 支持 32 位 size、64 位 largesize（size==1）、size==0（盒到父容器末尾）；
- 校验盒长度不小于盒头、且不超出父容器 / 文件边界，越界抛
  :class:`BoxLengthError`；
- 分片布局信号（moof/mvex）与加密信号在 parser 层明确拒绝。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Iterator

from ..errors import BoxLengthError

CONTAINER_TYPES = frozenset({"moov", "trak", "mdia", "minf", "stbl", "edts", "dinf"})
# 分片 MP4 的信号盒：moof 出现在顶层即拒绝；mvex 出现在 moov 内即拒绝。
FRAGMENT_SIGNAL_TOP = frozenset({"moof", "mfra"})
FRAGMENT_SIGNAL_MOOV = frozenset({"mvex"})
# 加密样本条目类型：本服务只支持非加密内容。
ENCRYPTED_SAMPLE_ENTRY_TYPES = frozenset({"encv", "enca", "enct"})
ENCRYPTION_BOX_TYPES = frozenset({"sinf", "saiz", "saio", "senc", "pssh"})


@dataclass(frozen=True)
class Box:
    """一个已定位的盒。

    ``start`` 为盒头起始绝对偏移，``size`` 含盒头，``header_size`` 为 8 或 16。
    有效载荷范围 = [start + header_size, start + size)。
    """

    type: str
    start: int
    size: int
    header_size: int

    @property
    def payload_start(self) -> int:
        return self.start + self.header_size

    @property
    def payload_end(self) -> int:
        return self.start + self.size

    def payload(self, data: bytes) -> bytes:
        return data[self.payload_start : self.payload_end]


def iter_boxes(data: bytes, start: int, end: int) -> Iterator[Box]:
    """在 ``data[start:end]`` 范围内顺序枚举同级盒。

    任何长度声明越界（超出文件 / 父容器）都抛 :class:`BoxLengthError`，
    绝不做截断式“尽量读”。
    """

    if start < 0 or end > len(data) or start > end:
        raise BoxLengthError(
            f"枚举范围非法: [{start}, {end})，文件长度 {len(data)}"
        )
    pos = start
    while pos < end:
        remaining = end - pos
        if remaining < 8:
            raise BoxLengthError(
                f"偏移 {pos} 处剩余 {remaining} 字节，不足 8 字节盒头"
            )
        raw_size = struct.unpack_from(">I", data, pos)[0]
        box_type = data[pos + 4 : pos + 8].decode("latin1")
        header_size = 8
        if raw_size == 1:
            # 64 位 largesize
            if remaining < 16:
                raise BoxLengthError(
                    f"偏移 {pos} 处的 '{box_type}' 盒声明为 largesize，"
                    f"但剩余 {remaining} 字节不足 16 字节盒头"
                )
            box_size = struct.unpack_from(">Q", data, pos + 8)[0]
            header_size = 16
        elif raw_size == 0:
            # size==0：盒体延伸到父容器末尾
            box_size = remaining
        else:
            box_size = raw_size

        if box_size < header_size:
            raise BoxLengthError(
                f"偏移 {pos} 处的 '{box_type}' 盒声明长度 {box_size} "
                f"小于盒头 {header_size} 字节"
            )
        if pos + box_size > end:
            raise BoxLengthError(
                f"偏移 {pos} 处的 '{box_type}' 盒声明长度 {box_size}，"
                f"终点 {pos + box_size} 超出边界 {end}（文件长度 {len(data)}）"
            )
        yield Box(box_type, pos, box_size, header_size)
        pos += box_size


def find_child(data: bytes, parent: Box, box_type: str) -> Box | None:
    """在父容器的直接子盒中找第一个匹配的盒（不存在返回 None）。"""

    for box in iter_boxes(data, parent.payload_start, parent.payload_end):
        if box.type == box_type:
            return box
    return None


def find_path(data: bytes, root: Box, *path: str) -> Box | None:
    """按直接层级路径找盒，任一层缺失返回 None。"""

    current: Box | None = root
    for name in path:
        if current is None:
            return None
        current = find_child(data, current, name)
    return current
