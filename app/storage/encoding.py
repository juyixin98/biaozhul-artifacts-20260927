"""posting 块的二进制编解码（SQLite BLOB 持久化用）。

每个块存为小端 8 字节无符号整数序列；块上界单独成列，供安全跳跃，
不必反序列化整块即可判断是否可跳过。
"""
from __future__ import annotations

import struct

from ..index.posting import Block

_UINT64_MAX = 2**64 - 1


def encode_ids(ids) -> bytes:
    for x in ids:
        if not isinstance(x, int) or isinstance(x, bool) or not (0 <= x <= _UINT64_MAX):
            raise ValueError(f"文档 ID 超出 uint64 范围：{x!r}")
    return struct.pack(f"<{len(ids)}Q", *ids)


def decode_ids(payload: bytes) -> tuple[int, ...]:
    if len(payload) % 8 != 0:
        raise ValueError(f"块 payload 长度必须是 8 的倍数，收到 {len(payload)}")
    return struct.unpack(f"<{len(payload) // 8}Q", payload)


def encode_block(block: Block) -> bytes:
    return encode_ids(block.ids)


def decode_block(payload: bytes) -> Block:
    ids = decode_ids(payload)
    return Block(ids, ids[-1])
