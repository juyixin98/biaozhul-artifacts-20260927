"""Deterministic local synthetic fixtures.

Nothing here touches a network or a production account. Running this module as
a script regenerates ``samples/fixtures/*.json``; the test suite imports the
same builder functions directly.
"""

from __future__ import annotations

import base64
import json
import struct
from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"

# A validity pattern that exercises byte boundaries: bits 6,7 of byte 0 and
# bits 0,1 of byte 1 are deliberately involved, including a NULL at index 7
# (byte boundary) and non-NULL at 8/9.
INT32_PRIMITIVE = {
    "type": "int32",
    "values": [0, 1, 2, 3, None, 5, 6, None, 8, 9],  # NULLs at 4 and 7
}

# Empty string and NULL adjacent, plus multi-byte UTF-8.
STRINGS = {
    "type": "utf8",
    "values": ["alpha", None, "", "βγ", "", None, "z", "δεζ"],
}

# Slice parameters used by the "non-zero offset" fixture.
SLICE_CASES = [
    {"type": "int32", "offset": 6, "length": 4, "expect": [6, None, 8, 9]},
    {"type": "utf8", "offset": 1, "length": 5, "expect": [None, "", "βγ", "", None]},
    {"type": "utf8", "offset": 5, "length": 3, "expect": [None, "z", "δεζ"]},
]


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def decreasing_offsets_descriptor() -> dict:
    # Offsets 0,3,2,5: slot 2 decreases (2 < 3); data is otherwise long enough.
    return {
        "type": "utf8",
        "length": 3,
        "offset": 0,
        "buffers": [None, _b64(struct.pack("<iiii", 0, 3, 2, 5)), _b64(b"abcde")],
        "expect_violations": ["DECREASING_OFFSET"],
    }


def out_of_bounds_offsets_descriptor() -> dict:
    return {
        "type": "utf8",
        "length": 2,
        "offset": 0,
        "buffers": [None, _b64(struct.pack("<iii", 0, 3, 10)), _b64(b"abcde")],
        "expect_violations": ["OFFSET_OUT_OF_BOUNDS"],
    }


def short_data_descriptor() -> dict:
    return {
        "type": "int32",
        "length": 6,
        "offset": 0,
        "buffers": [None, _b64(b"\x00" * 20)],
        "expect_violations": ["BUFFER_TOO_SHORT"],
    }


def padding_descriptor() -> dict:
    # length 6 but padding bits 6,7 of the validity byte are set.
    return {
        "type": "int32",
        "length": 6,
        "offset": 0,
        "buffers": [_b64(bytes([0xFF])), _b64(b"\x00" * 24)],
        "expect_violations": ["INVALID_PADDING"],
    }


def invalid_utf8_descriptor() -> dict:
    return {
        "type": "utf8",
        "length": 1,
        "offset": 0,
        "buffers": [None, _b64(struct.pack("<ii", 0, 1)), _b64(b"\xff")],
        "expect_violations": ["UTF8_INVALID"],
    }


def nonzero_offset_valid_descriptor() -> dict:
    # Producer array of 3 slots, validity [1,0,1] (NULL at 1); import as view
    # at offset=1, length=2 -> [NULL, "b"].
    return {
        "type": "utf8",
        "length": 2,
        "offset": 1,
        "buffers": [
            _b64(bytes([0b00000101])),
            _b64(struct.pack("<iiii", 0, 1, 1, 2)),
            _b64(b"ab"),
        ],
        "expect_values": [None, "b"],
    }


MALFORMED_DESCRIPTORS = {
    "decreasing_offsets": decreasing_offsets_descriptor(),
    "out_of_bounds_offsets": out_of_bounds_offsets_descriptor(),
    "short_data": short_data_descriptor(),
    "padding_bits": padding_descriptor(),
    "invalid_utf8": invalid_utf8_descriptor(),
    "nonzero_offset_view": nonzero_offset_valid_descriptor(),
}


def build_all() -> dict:
    return {
        "primitive": INT32_PRIMITIVE,
        "strings": STRINGS,
        "slice_cases": SLICE_CASES,
        "malformed": MALFORMED_DESCRIPTORS,
    }


def write_fixtures() -> Path:
    data = build_all()
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    out = FIXTURE_DIR / "fixtures.json"
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return out


if __name__ == "__main__":
    path = write_fixtures()
    print(f"wrote {path}")
