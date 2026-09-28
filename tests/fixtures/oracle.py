"""Independent oracle and synthetic fixture builders.

Nothing here calls the implementation under test to *generate* expected
answers: expected values, validity bytes and offset tables are assembled
directly with ``struct`` and pure-Python bit math. The same buffer layouts can
then be fed to the importer and to PyArrow, and the three decodings compared.
"""
from __future__ import annotations

import base64
import struct

from app.core.bitmath import pack_validity

_FMT = {
    "int8": "<b", "uint8": "B",
    "int16": "<h", "uint16": "<H",
    "int32": "<i", "uint32": "<I",
    "int64": "<q", "uint64": "<Q",
    "float32": "<f", "float": "<f",
    "float64": "<d", "double": "<d",
}
_WIDTH = {"int8": 1, "uint8": 1, "int16": 2, "uint16": 2, "int32": 4,
          "uint32": 4, "int64": 8, "uint64": 8,
          "float32": 4, "float": 4, "float64": 8, "double": 8}


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def fixed_descriptor(type_name: str, values: list[int | None], *,
                     null_count: int | None = None,
                     trailing_bits: bool = False,
                     extra_data: int = 0,
                     drop_data_bytes: int = 0) -> dict:
    """Build a raw descriptor for a fixed-width column entirely by hand."""
    width = _WIDTH[type_name]
    flags = [v is not None for v in values]
    validity = pack_validity(flags) if not all(flags) else None
    if validity is not None and trailing_bits:
        validity = bytearray(validity)
        validity[-1] |= 0x80  # force a padding bit high
        validity = bytes(validity)
    data = bytearray()
    for v in values:
        if v is None:
            data.extend(b"\x00" * width)  # Arrow leaves null data slots arbitrary
        else:
            data.extend(struct.pack(_FMT[type_name], v))
    if extra_data:
        data.extend(b"\xAB" * extra_data)
    if drop_data_bytes:
        data = data[:-drop_data_bytes]
    desc = {
        "type": type_name,
        "length": len(values),
        "data": b64(bytes(data)),
    }
    if validity is not None:
        desc["validity"] = b64(validity)
        desc["null_count"] = flags.count(False) if null_count is None else null_count
    return desc


def string_descriptor(values: list[str | None], *,
                      null_count: int | None = None,
                      trailing_bits: bool = False,
                      mutate=None) -> dict:
    """Build a utf8 descriptor: data blob + int32 offsets, by hand.

    ``mutate(offsets_list, data_bytearray)`` may corrupt them for negative tests;
    the offsets are re-packed exactly as mutated.
    """
    flags = [v is not None for v in values]
    validity = pack_validity(flags) if not all(flags) else None
    if validity is not None and trailing_bits:
        validity = bytearray(validity)
        validity[-1] |= 0x80
        validity = bytes(validity)

    data = bytearray()
    offsets = [0]
    for v in values:
        chunk = b"" if v is None else v.encode("utf-8")
        data.extend(chunk)
        offsets.append(len(data))
    if mutate is not None:
        mutate(offsets, data)
    offsets_raw = struct.pack(f"<{len(offsets)}i", *offsets)
    desc = {
        "type": "utf8",
        "length": len(values),
        "data": b64(bytes(data)),
        "offsets": b64(offsets_raw),
    }
    if validity is not None:
        desc["validity"] = b64(validity)
        desc["null_count"] = flags.count(False) if null_count is None else null_count
    return desc


def expected_fixed(type_name: str, values: list[int | None]) -> list:
    """Independent expected decode (passes through Python's own struct)."""
    out = []
    for v in values:
        if v is None:
            out.append(None)
        else:
            raw = struct.pack(_FMT[type_name], v)
            out.append(struct.unpack(_FMT[type_name], raw)[0])
    return out


def expected_strings(values: list[str | None]) -> list[str | None]:
    return list(values)
