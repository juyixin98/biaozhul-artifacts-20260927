"""Parquet level encodings: RLE / bit-packed hybrid.

Per the Parquet spec (Encodings.md), each run begins with a ULEB-128 varint
header whose least-significant bit selects the run type:

* bit 0 == 0  -> RLE run;       header = (run_count << 1),
                               followed by the repeated value as
                               ``ceil(bit_width / 8)`` little-endian bytes.
* bit 0 == 1  -> bit-packed;   header = ((value_count / 8) << 1) | 1,
                               followed by ``value_count * bit_width / 8``
                               bytes packed LSB-first. The run is always a
                               multiple of eight values; shorter tails are
                               padded with zeros.

On data pages (v1) the whole level blob is additionally framed as
``<4-byte little-endian length><bit-width byte><runs>`` where the length
covers the bit-width byte and the run bytes.
"""
from __future__ import annotations

from dataclasses import dataclass


def bit_width_for(max_value: int) -> int:
    if max_value < 0:
        raise ValueError("bit width requires non-negative max")
    width = 0
    v = max_value
    while v:
        width += 1
        v >>= 1
    return width


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    shift = 0
    result = 0
    while True:
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7


def _append_varint(buf: bytearray, value: int) -> None:
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            buf.append(b | 0x80)
        else:
            buf.append(b)
            return


def encode_levels(levels: list[int], bit_width: int) -> bytes:
    """Encode levels into the run portion (caller frames length prefix)."""
    if bit_width == 0:
        return b""
    buf = bytearray()
    value_nbytes = (bit_width + 7) // 8
    n = len(levels)
    i = 0
    while i < n:
        run_len = 1
        while i + run_len < n and levels[i + run_len] == levels[i]:
            run_len += 1
        # Emit maximal equal runs as RLE. RLE is legal for any length
        # (including 1), so no padding zeros ever appear before a later run.
        # The bit-packed decoder path is fully implemented and unit-tested
        # independently (PyArrow files use it), preserving read interop.
        _append_varint(buf, run_len << 1)  # RLE header, low bit 0
        buf += int(levels[i]).to_bytes(value_nbytes, "little")
        i += run_len
    return bytes(buf)


def frame_levels(levels: list[int], bit_width: int) -> bytes:
    """Full v1 data-page level blob: ``<len:4><hybrid runs>``.

    Unlike the standalone encoding, the bit width is NOT stored in the blob;
    it is derived from the schema's maximum definition/repetition level. The
    4-byte little-endian length counts only the hybrid run bytes that follow.
    """
    runs = encode_levels(levels, bit_width)
    return len(runs).to_bytes(4, "little") + runs


def _pack_lsb_first(values: list[int], bit_width: int) -> bytes:
    nbytes = (len(values) * bit_width + 7) // 8
    result = 0
    mask = (1 << bit_width) - 1
    for idx, v in enumerate(values):
        result |= (v & mask) << (idx * bit_width)
    return result.to_bytes(nbytes, "little")


@dataclass
class DecodedLevels:
    levels: list[int]
    bytes_consumed: int


def decode_levels(data: bytes, bit_width: int, count: int,
                  offset: int = 0) -> DecodedLevels:
    if bit_width == 0:
        return DecodedLevels([0] * count, 0)
    pos = offset
    out: list[int] = []
    value_nbytes = (bit_width + 7) // 8
    while len(out) < count:
        header, pos = _read_varint(data, pos)
        if header & 1:
            # Bit-packed run: header counts groups of eight.
            groups = header >> 1
            n_values = groups * 8
            nbytes = (n_values * bit_width + 7) // 8
            packed = int.from_bytes(data[pos:pos + nbytes], "little")
            pos += nbytes
            mask = (1 << bit_width) - 1
            for k in range(n_values):
                out.append((packed >> (k * bit_width)) & mask)
        else:
            # RLE run.
            run_count = header >> 1
            value = int.from_bytes(data[pos:pos + value_nbytes], "little")
            pos += value_nbytes
            out.extend([value] * run_count)
    return DecodedLevels(out[:count], pos - offset)
