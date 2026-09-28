"""MPEG-2 CRC-32 (ISO/IEC 13818-1 Annex A / ITU-T H.222).

Polynomial 0x04C11DB7, init 0xFFFFFFFF, no reflection, no final xor.

This is a table-driven implementation used by the *analyzer*. The test
fixtures contain an independent bitwise implementation; the two never
share code, so a CRC test failure cannot be caused by a shared bug.
"""
from __future__ import annotations

_POLY = 0x04C11DB7
_MASK = 0xFFFFFFFF


def _build_table() -> tuple[int, ...]:
    table: list[int] = []
    for index in range(256):
        crc = index << 24
        for _ in range(8):
            if crc & 0x80000000:
                crc = ((crc << 1) ^ _POLY) & _MASK
            else:
                crc = (crc << 1) & _MASK
        table.append(crc)
    return tuple(table)


_TABLE = _build_table()


def mpeg_crc32(data: bytes | bytearray | memoryview) -> int:
    """Return the MPEG-2 CRC-32 of ``data``."""
    crc = 0xFFFFFFFF
    for byte in data:
        crc = ((crc << 8) & _MASK) ^ _TABLE[((crc >> 24) ^ byte) & 0xFF]
    return crc
