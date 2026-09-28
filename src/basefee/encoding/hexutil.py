"""Hexadecimal and integer-byte helpers shared by the encoding layer."""

from __future__ import annotations


class HexError(ValueError):
    """Raised when 0x-hex data cannot be decoded."""

    def __init__(self, message: str, code: str = "E001_HEX_DECODE"):
        super().__init__(message)
        self.code = code


def strip_0x(value: str) -> str:
    if not isinstance(value, str):
        raise HexError("expected hex string")
    v = value[2:] if value.startswith("0x") or value.startswith("0X") else value
    if v == "":
        return ""
    if len(v) % 2:
        raise HexError("odd-length hex string")
    return v


def hex_to_bytes(value: str) -> bytes:
    v = strip_0x(value)
    try:
        return bytes.fromhex(v)
    except ValueError as exc:
        raise HexError(f"invalid hex characters: {exc}") from exc


def bytes_to_hex(value: bytes) -> str:
    return "0x" + value.hex()


def int_to_minimal_bytes(value: int) -> bytes:
    """Minimal big-endian encoding required by RLP scalar conventions."""
    if value < 0:
        raise ValueError("cannot encode negative integer")
    return value.to_bytes((value.bit_length() + 7) // 8, "big") if value else b""


def encode_int(value: int) -> bytes:
    return int_to_minimal_bytes(value)


def decode_int(data: bytes) -> int:
    if not isinstance(data, bytes):
        raise HexError("expected byte string for integer", "E002_RLP_DECODE")
    if data and data[0] == 0:
        raise HexError("non-canonical integer with leading zero", "E003_CANONICAL_ENCODING")
    return int.from_bytes(data, "big")
