"""Unit tests for the format adapters: pylist, IPC stream, raw buffers."""

from __future__ import annotations

import base64
import struct

import pytest

from arrowzero.adapters import (
    FormatError,
    export_ipc_stream,
    fingerprint_buffers,
    import_ipc_stream,
    import_pylist,
    import_raw_buffers,
)
from arrowzero.kernel.checks import ValidationError

pytestmark = pytest.mark.unit


def test_pylist_import_copies_and_validates():
    view, ev = import_pylist(["a", None, ""], "utf8")
    assert view.to_pylist() == ["a", None, ""]
    assert ev["format"] == "pylist"
    assert ev["zero_copy"] is False
    assert ev["copied_bytes"] > 0
    assert {f["name"] for f in ev["buffer_fingerprints"]} == {
        "validity", "offsets", "data"
    }


def test_ipc_import_is_zero_copy_inside_payload():
    view, _ = import_pylist(["a", None, "", "bc"], "utf8")
    payload = export_ipc_stream(view)
    imported, ev = import_ipc_stream(payload)
    assert ev["zero_copy"] is True
    assert ev["copied_bytes"] == 0
    assert len(ev["buffers_inside_payload"]) == 3
    for info in ev["buffers_inside_payload"]:
        assert 0 <= info["payload_relative_offset"] < ev["payload_size"]
    assert imported.slice(1, 3).to_pylist() == [None, "", "bc"]


def test_ipc_export_roundtrip_primitive():
    view, _ = import_pylist([1, None, 3], "int16")
    payload = export_ipc_stream(view)
    imported, ev = import_ipc_stream(payload)
    assert ev["zero_copy"] is True
    assert imported.to_pylist() == [1, None, 3]


def test_ipc_rejects_garbage():
    with pytest.raises(FormatError):
        import_ipc_stream(b"not arrow at all")


def test_raw_import_nonzero_offset_descriptor():
    validity = base64.b64encode(bytes([0b00000101])).decode()  # [1,0,1]
    offsets = base64.b64encode(struct.pack("<iiii", 0, 1, 1, 2)).decode()
    data = base64.b64encode(b"ab").decode()
    view, ev = import_raw_buffers(
        {"type": "utf8", "length": 2, "offset": 1,
         "buffers": [validity, offsets, data]}
    )
    assert view.offset == 1
    assert view.to_pylist() == [None, "b"]
    assert ev["zero_copy"] is True
    assert ev["copied_bytes"] == 0


def test_raw_import_rejects_decreasing_offsets_with_category():
    desc = {
        "type": "utf8", "length": 3,
        "buffers": [
            None,
            base64.b64encode(struct.pack("<iiii", 0, 3, 2, 5)).decode(),
            base64.b64encode(b"abcde").decode(),
        ],
    }
    with pytest.raises(ValidationError) as exc:
        import_raw_buffers(desc)
    codes = [v["code"] for v in exc.value.to_dicts()]
    assert codes == ["DECREASING_OFFSET"]


def test_raw_import_rejects_short_data():
    desc = {
        "type": "int32", "length": 6,
        "buffers": [None, base64.b64encode(b"\x00" * 20).decode()],
    }
    with pytest.raises(ValidationError) as exc:
        import_raw_buffers(desc)
    assert exc.value.to_dicts()[0]["code"] == "BUFFER_TOO_SHORT"


def test_raw_import_bad_base64_is_format_error():
    with pytest.raises(FormatError):
        import_raw_buffers(
            {"type": "int32", "length": 1, "buffers": [None, "%%%notbase64"]}
        )


def test_unknown_type_is_format_error():
    with pytest.raises(FormatError):
        import_pylist([True], "bool")


def test_fingerprints_stable_for_same_bytes():
    a, _ = import_pylist([1, 2], "int32")
    b, _ = import_pylist([1, 2], "int32")
    fa = {f["name"]: f["sha256_16"] for f in fingerprint_buffers(a)}
    fb = {f["name"]: f["sha256_16"] for f in fingerprint_buffers(b)}
    assert fa == fb
    assert all(v is None or len(v) == 16 for v in fa.values())
