"""RLE / bit-packed hybrid codec for definition and repetition levels.

Parquet stores level streams on the data page with the hybrid encoding
(Parquet parquet.thrift ``RleBitmap``):

* runs are a header ``(count << 1) | 0`` followed by the value;
* bit-packed groups pack 8 values LSB-first with header ``(groups << 1) | 1``.

A DataPage v1 prefixes the buffer with a 4-byte little-endian byte length;
DataPage v2 carries the length in the page header instead.

This implementation is intentionally self-contained pure Python so it can be
cross-checked against *both* independent implementations (fastparquet's
cython reader and PyArrow's writer) in the tests -- the kernel's level
semantics never depend on the reference implementation.
"""
from __future__ import annotations

from typing import Iterable


def bit_width(max_level: int) -> int:
    if max_level < 0:
        raise ValueError("max_level must be >= 0")
    if max_level == 0:
        return 0
    return (max_level - 1).bit_length() + 1 if False else max_level.bit_length()


def _varint_encode(value: int) -> bytes:
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _varint_decode(buf: bytes, pos: int) -> tuple[int, int]:
    shift = 0
    result = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7


def _pack_group(values: list[int], width: int) -> bytes:
    """Pack 8 values LSB-first into ``width`` bytes (values are consecutive:
    value i occupies bit positions ``[i*width, (i+1)*width)``)."""
    bits = [0] * (width * 8)
    for i, v in enumerate(values[:8]):
        for k in range(width):
            bits[i * width + k] = (v >> k) & 1
    out = bytearray(width)
    for idx, bit in enumerate(bits):
        if bit:
            out[idx // 8] |= 1 << (idx % 8)
    return bytes(out)


def _unpack_group(data: bytes, width: int, count: int) -> list[int]:
    """Inverse of :func:`_pack_group` for one group's ``width`` bytes."""
    values = []
    for i in range(min(count, 8)):
        v = 0
        base = i * width
        for k in range(width):
            idx = base + k
            if (data[idx // 8] >> (idx % 8)) & 1:
                v |= 1 << k
        values.append(v)
    return values


def encode_hybrid(levels: Iterable[int], width: int) -> bytes:
    """Encode levels without the DataPage-v1 length prefix.

    Conformant strategy:

    * a run of >= 8 equal values is emitted as one RLE run;
    * other values are packed into groups of exactly 8, EXCEPT the final
      group of the stream which may be partial (Parquet allows padding only
      there);
    * leftover values that would form a short middle "group" (0 < len < 8
      before another run) are emitted value-by-value as length-1 RLE runs --
      a padded middle group would be decoded as 8 real levels.
    """
    levels = list(levels)
    if width == 0:
        return b""
    for v in levels:
        if v < 0 or v >= (1 << width):
            raise ValueError(f"level {v} does not fit in {width} bits")

    buf = bytearray()
    n = len(levels)
    i = 0

    def emit_rle(value: int, count: int) -> None:
        buf.extend(_varint_encode((count << 1) | 0))
        buf.append(value)

    def emit_group(group: list[int]) -> None:
        buf.extend(_varint_encode((1 << 1) | 1))
        buf.extend(_pack_group(group, width))

    while i < n:
        j = i + 1
        while j < n and levels[j] == levels[i]:
            j += 1
        if j - i >= 8:
            emit_rle(levels[i], j - i)
            i = j
            continue
        # Short run / mixed region: pack full groups of 8 until the next
        # long RLE run, leaving any sub-group-of-8 remainder to be encoded
        # as single-value RLE runs (unless it is the stream tail, where a
        # partial bit-packed group is legal).
        start = i
        k = i
        while k < n:
            m = k + 1
            while m < n and levels[m] == levels[k]:
                m += 1
            if m - k >= 8:
                break
            k = m
        chunk = levels[start:k]
        full, rem = divmod(len(chunk), 8)
        for g in range(full):
            emit_group(chunk[g * 8:(g + 1) * 8])
        tail = chunk[full * 8:]
        at_stream_end = k == n
        if rem and at_stream_end:
            emit_group(tail)  # legal partial final group
        else:
            for v in tail:
                emit_rle(v, 1)
        i = k
    return bytes(buf)


def encode_levels_v1(levels: Iterable[int], max_level: int) -> bytes:
    """DataPage v1 layout: 4-byte LE length prefix + hybrid payload."""
    width = bit_width(max_level)
    payload = encode_hybrid(levels, width)
    return len(payload).to_bytes(4, "little") + payload


def encode_levels_v2(levels: Iterable[int], max_level: int) -> bytes:
    """DataPage v2 layout: raw hybrid payload (length lives in the header)."""
    width = bit_width(max_level)
    return encode_hybrid(levels, width)


def decode_hybrid(buf: bytes, width: int, count: int) -> list[int]:
    """Decode exactly ``count`` levels from a hybrid payload."""
    if width == 0:
        return [0] * count
    out: list[int] = []
    pos = 0
    while len(out) < count and pos < len(buf):
        header, pos = _varint_decode(buf, pos)
        if header & 1:
            groups = header >> 1
            # Each group packs 8 values into ``width`` bytes.
            nbytes = groups * width
            chunk = buf[pos:pos + nbytes]
            if len(chunk) != nbytes:
                raise ValueError("truncated bit-packed level group")
            pos += nbytes
            # Groups are independent bit containers (padding in the last
            # group does not shift the next group).
            remaining = count - len(out)
            for g in range(groups):
                if remaining <= 0:
                    break
                group_bytes = chunk[g * width:(g + 1) * width]
                have = min(8, remaining)
                out += _unpack_group(group_bytes, width, have)
                remaining -= have
        else:
            run_len = header >> 1
            value = buf[pos]
            pos += 1
            out += [value] * run_len
    return out[:count]


def decode_levels_v1(buf: bytes, max_level: int, count: int,
                     offset: int = 0) -> tuple[list[int], int]:
    """Decode a v1 prefixed stream; returns (levels, new_offset)."""
    if max_level == 0:
        return [0] * count, offset
    import struct
    (length,) = struct.unpack_from("<I", buf, offset)
    start = offset + 4
    payload = buf[start:start + length]
    levels = decode_hybrid(payload, bit_width(max_level), count)
    return levels, start + length
