"""Minimal, self-contained RLP (Recursive Length Prefix) codec.

Implements exactly the encoding defined in Appendix B of the Ethereum Yellow
Paper. RLP is the canonical serialization used for signing digests and block
hashing in this synthetic model. Canonical (shortest-form) encoding is enforced
on decode so a non-canonical payload is rejected with E003 rather than accepted.
"""

from __future__ import annotations

from typing import List, Sequence, Union

RLPItem = Union[bytes, Sequence["RLPItem"]]

_OFFSET_SINGLE_BYTE = 0x00
_OFFSET_SHORT_STRING = 0x80
_OFFSET_LONG_STRING = 0xB7
_OFFSET_SHORT_LIST = 0xC0
_OFFSET_LONG_LIST = 0xF7


class RLPError(ValueError):
    """Raised on malformed RLP input."""


def encode(item: RLPItem) -> bytes:
    if isinstance(item, bytes):
        return _encode_bytes(item)
    if isinstance(item, bytearray):
        return _encode_bytes(bytes(item))
    if isinstance(item, (list, tuple)):
        payload = b"".join(encode(x) for x in item)
        return _encode_length(len(payload), _OFFSET_SHORT_LIST) + payload
    raise TypeError(f"RLP cannot encode {type(item)!r}; only bytes/list/tuple")


def _encode_bytes(data: bytes) -> bytes:
    if len(data) == 1 and data[0] < _OFFSET_SHORT_STRING:
        return data
    return _encode_length(len(data), _OFFSET_SHORT_STRING) + data


def _encode_length(length: int, offset: int) -> bytes:
    if length < 56:
        return bytes([offset + length])
    bl = length.to_bytes((length.bit_length() + 7) // 8, "big")
    return bytes([offset + 55 + len(bl)]) + bl


def decode(raw: bytes) -> RLPItem:
    if not isinstance(raw, (bytes, bytearray)):
        raise RLPError("input must be bytes")
    item, end = _decode_at(bytes(raw), 0)
    if end != len(raw):
        raise RLPError("trailing bytes after RLP item (non-canonical framing)")
    return item


def decode_list(raw: bytes) -> List[RLPItem]:
    item = decode(raw)
    if not isinstance(item, list):
        raise RLPError("expected top-level list")
    return item


def _decode_at(raw: bytes, idx: int):
    if idx >= len(raw):
        raise RLPError("unexpected end of input")
    prefix = raw[idx]
    if prefix < _OFFSET_SHORT_STRING:
        return bytes([prefix]), idx + 1
    if prefix < _OFFSET_LONG_STRING:
        length = prefix - _OFFSET_SHORT_STRING
        start = idx + 1
        _check_canonical_string(raw, start, length)
        return raw[start : start + length], start + length
    if prefix < _OFFSET_SHORT_LIST:
        len_len = prefix - _OFFSET_LONG_STRING
        start = idx + 1 + len_len
        length = _read_len(raw, idx + 1, len_len)
        _check_canonical_string(raw, start, length)
        return raw[start : start + length], start + length
    if prefix < _OFFSET_LONG_LIST:
        length = prefix - _OFFSET_SHORT_LIST
        start = idx + 1
        return _decode_list_body(raw, start, length), start + length
    len_len = prefix - _OFFSET_LONG_LIST
    start = idx + 1 + len_len
    length = _read_len(raw, idx + 1, len_len)
    return _decode_list_body(raw, start, length), start + length


def _read_len(raw: bytes, at: int, len_len: int) -> int:
    if len_len <= 0 or len_len > 8:
        raise RLPError("invalid length-of-length")
    if at + len_len > len(raw):
        raise RLPError("truncated length prefix")
    value = int.from_bytes(raw[at : at + len_len], "big")
    if value < 56:
        raise RLPError("non-canonical long length (short form required)")
    if raw[at] == 0:
        raise RLPError("non-canonical leading zero in length")
    return value


def _check_canonical_string(raw: bytes, start: int, length: int) -> None:
    if start + length > len(raw):
        raise RLPError("string overruns input")
    if length == 1 and start < len(raw) and raw[start] < _OFFSET_SHORT_STRING:
        raise RLPError("non-canonical single byte (must be bare)")


def _decode_list_body(raw: bytes, start: int, length: int) -> List[RLPItem]:
    end = start + length
    if end > len(raw):
        raise RLPError("list overruns input")
    out: List[RLPItem] = []
    idx = start
    while idx < end:
        item, idx = _decode_at(raw, idx)
        out.append(item)
    if idx != end:
        raise RLPError("list length does not match payload")
    return out
