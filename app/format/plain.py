"""PLAIN value encoding for the restricted physical type set.

No dictionaries, no DELTA encodings: data pages use PLAIN so the byte layout
is fully specified here and independently checkable.

* BOOLEAN      bit-packed LSB-first, one bit per value (last byte padded)
* INT32        4-byte little-endian two's complement
* INT64        8-byte little-endian two's complement
* FLOAT        4-byte IEEE 754 little-endian
* DOUBLE       8-byte IEEE 754 little-endian
* BYTE_ARRAY   <4-byte little-endian length><utf-8 bytes>
"""
from __future__ import annotations

import struct

from ..kernel.schema import PhysicalType


class PlainEncodingError(ValueError):
    pass


def encode_values(values: list, physical: PhysicalType) -> bytes:
    if physical == PhysicalType.BOOLEAN:
        return _encode_booleans(values)
    if physical == PhysicalType.INT32:
        return b"".join(struct.pack("<i", int(v)) for v in values)
    if physical == PhysicalType.INT64:
        return b"".join(struct.pack("<q", int(v)) for v in values)
    if physical == PhysicalType.FLOAT:
        return b"".join(struct.pack("<f", float(v)) for v in values)
    if physical == PhysicalType.DOUBLE:
        return b"".join(struct.pack("<d", float(v)) for v in values)
    if physical == PhysicalType.BYTE_ARRAY:
        out = bytearray()
        for v in values:
            if not isinstance(v, str):
                raise PlainEncodingError(f"BYTE_ARRAY expects str, got {type(v)}")
            data = v.encode("utf-8")
            out += len(data).to_bytes(4, "little") + data
        return bytes(out)
    raise PlainEncodingError(f"PLAIN encoding unsupported for {physical}")


def _encode_booleans(values: list) -> bytes:
    out = bytearray()
    for i, v in enumerate(values):
        if i % 8 == 0:
            out.append(0)
        if v:
            out[i // 8] |= 1 << (i % 8)
    return bytes(out)


def decode_values(data: bytes, physical: PhysicalType, count: int,
                  offset: int = 0) -> tuple[list, int]:
    if physical == PhysicalType.BOOLEAN:
        return _decode_booleans(data, count, offset)
    if physical == PhysicalType.INT32:
        return _decode_struct(data, "<i", 4, count, offset)
    if physical == PhysicalType.INT64:
        return _decode_struct(data, "<q", 8, count, offset)
    if physical == PhysicalType.FLOAT:
        return _decode_struct(data, "<f", 4, count, offset)
    if physical == PhysicalType.DOUBLE:
        return _decode_struct(data, "<d", 8, count, offset)
    if physical == PhysicalType.BYTE_ARRAY:
        out: list[str] = []
        pos = offset
        for _ in range(count):
            (length,) = struct.unpack_from("<I", data, pos)
            pos += 4
            out.append(data[pos:pos + length].decode("utf-8"))
            pos += length
        return out, pos
    raise PlainEncodingError(f"PLAIN decoding unsupported for {physical}")


def _decode_struct(data, fmt, width, count, offset):
    out = [struct.unpack_from(fmt, data, offset + i * width)[0]
           for i in range(count)]
    return out, offset + count * width


def _decode_booleans(data: bytes, count: int, offset: int) -> tuple[list, int]:
    out: list[bool] = []
    for i in range(count):
        byte = data[offset + i // 8]
        out.append(bool((byte >> (i % 8)) & 1))
    nbytes = (count + 7) // 8
    return out, offset + nbytes
