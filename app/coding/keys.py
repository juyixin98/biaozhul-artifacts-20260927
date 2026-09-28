"""固定键宽键的解析与位读取。"""
from __future__ import annotations


def normalize_key(key: bytes | bytearray | str, key_len: int) -> bytes:
    """统一为定长 bytes；hex 字符串可带或不带 0x。键宽不符直接拒绝。"""
    if isinstance(key, str):
        k = key[2:] if key.startswith("0x") else key
        try:
            raw = bytes.fromhex(k)
        except ValueError as exc:
            raise ValueError(f"键不是合法 hex: {exc}") from exc
    else:
        raw = bytes(key)
    if len(raw) != key_len:
        raise ValueError(f"键宽必须为 {key_len} 字节，收到 {len(raw)} 字节")
    return raw


def normalize_value(value: bytes | bytearray | str | None) -> bytes | None:
    """``None`` 表示删除（键不存在）；空字节串是合法值，表示“存在且值为空”。"""
    if value is None:
        return None
    if isinstance(value, str):
        v = value[2:] if value.startswith("0x") else value
        try:
            return bytes.fromhex(v)
        except ValueError as exc:
            raise ValueError(f"值不是合法 hex: {exc}") from exc
    return bytes(value)


def bit_at(key: bytes, depth: int) -> int:
    """读取键在 depth 层的分支位：MSB 优先。"""
    return (key[depth // 8] >> (7 - (depth % 8))) & 1


def first_divergence(a: bytes, b: bytes, depth: int, max_depth: int) -> int:
    """两键从 depth 起第一个不同位的层号；到 max_depth 仍相同则返回 max_depth。"""
    d = depth
    while d < max_depth and bit_at(a, d) == bit_at(b, d):
        d += 1
    return d
