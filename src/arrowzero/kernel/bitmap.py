"""Validity bitmap operations.

Bit layout follows Arrow: bit ``i`` represents element ``i`` (LSB-first inside
each byte). These helpers are written in pure Python and independently
unit-tested; the kernel never infers NULL-ness from pyarrow accessors.
"""

from __future__ import annotations


def is_bit_set(buf: bytes | bytearray | memoryview, index: int) -> bool:
    """Return validity bit at ``index`` for an array whose logical offset is 0."""
    byte = buf[index >> 3]
    return bool((byte >> (index & 7)) & 1)


def count_set_bits(buf: bytes | bytearray | memoryview, length: int, offset: int = 0) -> int:
    """Count set bits for ``length`` elements starting at logical ``offset``."""
    total = 0
    for i in range(offset, offset + length):
        if is_bit_set(buf, i):
            total += 1
    return total


def count_padding_errors(buf: bytes | bytearray | memoryview, length: int) -> list[int]:
    """Return indices of set padding bits beyond ``length`` in the final byte.

    Arrow requires the ``(8 - length % 8)`` trailing padding bits of the last
    validity byte to be zero. An empty/aligned array has no padding bits.
    """
    if length == 0 or length % 8 == 0:
        return []
    used = length & 7
    last = buf[(length - 1) >> 3]
    errors: list[int] = []
    for bit in range(used, 8):
        if (last >> bit) & 1:
            errors.append(bit)
    return errors


def set_bit(buf: bytearray, index: int) -> None:
    buf[index >> 3] |= 1 << (index & 7)


def build_validity(valid_flags: list[bool]) -> bytes | None:
    """Build an Arrow validity buffer, or None when every element is present."""
    if all(valid_flags):
        return None
    size = max(1, (len(valid_flags) + 7) // 8)
    out = bytearray(size)
    for i, present in enumerate(valid_flags):
        if present:
            set_bit(out, i)
    return bytes(out)
