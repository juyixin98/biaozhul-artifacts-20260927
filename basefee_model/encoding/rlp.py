"""Minimal, dependency-free RLP (Recursive Length Prefix) codec.

Implements the Ethereum yellow-paper RLP serialization exactly enough for
transaction envelopes: byte strings and (possibly nested) lists. This is a
real serializer/deserializer with strict decoding, not a demo -- malformed
input raises :class:`EncodingError`.

Reference: https://ethereum.org/en/developers/docs/data-structures-and-encoding/rlp/
"""

from __future__ import annotations

from ..errors import EncodingError, FailureCode

# Single-byte / prefix thresholds.
BYTE_OFFSET = 0x80          # short string
LIST_OFFSET = 0xC0         # short list
LONG_THRESHOLD = 56        # length at/above this uses the long form


def _encode_length(length: int, offset: int) -> bytes:
    if length < LONG_THRESHOLD:
        return bytes([offset + length])
    bl = length.to_bytes((length.bit_length() + 7) // 8, "big")
    if bl[0] == 0:  # pragma: no cover - to_bytes already strips leading zeros
        raise EncodingError("non-canonical length", code=FailureCode.MALFORMED_RLP)
    return bytes([offset + LONG_THRESHOLD + len(bl)]) + bl


def _encode_bytes(b: bytes) -> bytes:
    n = len(b)
    if n == 1 and b[0] < BYTE_OFFSET:
        return b
    return _encode_length(n, BYTE_OFFSET) + b


def _encode_list(items: list) -> bytes:
    payload = b"".join(encode_raw(item) for item in items)
    return _encode_length(len(payload), LIST_OFFSET) + payload


def encode_raw(item) -> bytes:
    """Encode ``bytes`` or a (nested) ``list`` of such to RLP bytes."""
    if isinstance(item, bytes):
        return _encode_bytes(item)
    if isinstance(item, (bytearray, memoryview)):
        return _encode_bytes(bytes(item))
    if isinstance(item, list):
        return _encode_list(item)
    raise EncodingError(
        f"RLP can only encode bytes/list, got {type(item).__name__}",
        code=FailureCode.MALFORMED_RLP,
    )


def encode(items: list) -> bytes:
    """Encode a top-level list of items."""
    return _encode_list(items)


def _decode_length(data: bytes, ix: int):
    """Return (kind, payload_start, payload_end, next_index).

    kind is 0 for string, 1 for list.
    """
    if ix >= len(data):
        raise EncodingError("truncated RLP", code=FailureCode.MALFORMED_RLP)
    prefix = data[ix]

    if prefix < BYTE_OFFSET:           # single byte
        return 0, ix, ix + 1, ix + 1
    if prefix < LIST_OFFSET:           # string
        if prefix < BYTE_OFFSET + LONG_THRESHOLD:
            length = prefix - BYTE_OFFSET
            start = ix + 1
        else:
            nlen = prefix - BYTE_OFFSET - LONG_THRESHOLD
            start = ix + 1 + nlen
            if start > len(data):
                raise EncodingError("truncated RLP string length",
                                    code=FailureCode.MALFORMED_RLP)
            length = int.from_bytes(data[ix + 1:start], "big")
            if length < LONG_THRESHOLD:
                raise EncodingError("non-canonical RLP length",
                                    code=FailureCode.MALFORMED_RLP)
        end = start + length
        if end > len(data):
            raise EncodingError("truncated RLP string payload",
                                code=FailureCode.MALFORMED_RLP)
        return 0, start, end, end
    # list
    if prefix < LIST_OFFSET + LONG_THRESHOLD:
        length = prefix - LIST_OFFSET
        start = ix + 1
    else:
        nlen = prefix - LIST_OFFSET - LONG_THRESHOLD
        start = ix + 1 + nlen
        if start > len(data):
            raise EncodingError("truncated RLP list length",
                                code=FailureCode.MALFORMED_RLP)
        length = int.from_bytes(data[ix + 1:start], "big")
        if length < LONG_THRESHOLD:
            raise EncodingError("non-canonical RLP length",
                                code=FailureCode.MALFORMED_RLP)
    end = start + length
    if end > len(data):
        raise EncodingError("truncated RLP list payload",
                            code=FailureCode.MALFORMED_RLP)
    return 1, start, end, end


def decode(data: bytes):
    """Decode RLP ``bytes`` into a nested list/bytes structure."""
    kind, start, end, _ = _decode_length(data, 0)
    if end != len(data):
        raise EncodingError("trailing bytes after RLP item",
                            code=FailureCode.MALFORMED_RLP)
    if kind == 0:
        return data[start:end]
    return _decode_list_payload(data, start, end)


def _decode_list_payload(data: bytes, start: int, end: int) -> list:
    items = []
    ix = start
    while ix < end:
        kind, istart, iend, ix_next = _decode_length(data, ix)
        if kind == 0:
            items.append(data[istart:iend])
        else:
            items.append(_decode_list_payload(data, istart, iend))
        ix = ix_next
    return items


def int_from_bytes(b: bytes) -> int:
    return int.from_bytes(b, "big") if b else 0


def int_to_bytes(value: int) -> bytes:
    if value < 0:
        raise EncodingError("cannot RLP-encode negative integer",
                            code=FailureCode.INVALID_FIELDS)
    return value.to_bytes((value.bit_length() + 7) // 8, "big") if value else b""
