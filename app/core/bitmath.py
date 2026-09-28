"""Validity bitmap math.

Implemented directly over ``bytes``/``bytearray`` rather than delegating to
PyArrow, because the bitmap checks must independently judge buffers handed in
by an external party. Bit ``i`` is the least-significant bit of byte
``i // 8`` (Arrow / LSB0 convention).
"""
from __future__ import annotations


def ceil_div8(n: int) -> int:
    return (n + 7) // 8


def bit_get(buf: bytes | bytearray | memoryview, i: int) -> int:
    return (buf[i >> 3] >> (i & 7)) & 1


def count_set_bits(buf: bytes | bytearray | memoryview, start: int, length: int) -> int:
    """Count set bits in the *logical* half-open range [start, start+length)."""
    total = 0
    for j in range(start, start + length):
        total += bit_get(buf, j)
    return total


def logical_value_count(buf: bytes | bytearray | memoryview, start: int, length: int) -> int:
    """Number of valid (1) slots in a logical range, honoring the physical start."""
    return count_set_bits(buf, start, length)


def trailing_padding_bits_are_zero(buf: bytes | bytearray | memoryview, span: int) -> bool:
    """Arrow spec: padding bits past ``span`` in the last used byte must be 0."""
    if span == 0:
        # A zero-length array must not carry a validity buffer at all in our
        # contract; the caller decides. Nothing to check here.
        return True
    remainder = span & 7
    if remainder == 0:
        return True
    last = buf[(span - 1) >> 3]
    mask = 0xFF ^ ((1 << remainder) - 1)
    return (last & mask) == 0


def pack_validity(flags: list[bool] | list[int]) -> bytes:
    """Pack a sequence of 0/1 flags into an Arrow LSB0 bitmap."""
    out = bytearray(ceil_div8(len(flags)))
    for i, flag in enumerate(flags):
        if flag:
            out[i >> 3] |= 1 << (i & 7)
    return bytes(out)
