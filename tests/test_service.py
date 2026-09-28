"""Service-level tests: transaction outcomes, audit rows, categories, run logs."""

from __future__ import annotations

import pytest

from arrowzero.service import ViewService
from helpers import make_run_id

pytestmark = pytest.mark.unit


def _import(service: ViewService, run_id: str, type_name: str, values):
    return service.import_array(
        {"format": "pylist", "type": type_name, "values": values}, run_id=run_id
    )


def test_import_slice_concat_happy_path_commits_audit(
    service: ViewService, store, test_log, run_id: str
):
    test_log.step(run_id, "import", "started", input=[0, None, 2, 3])
    r1 = _import(service, run_id, "int32", [0, None, 2, 3])
    assert r1.status == "committed"
    test_log.step(run_id, "slice", "committed", handle=r1.payload["handle"])

    r2 = service.slice_array(r1.payload["handle"], 1, 3, run_id=run_id)
    assert r2.status == "committed"
    assert r2.payload["zero_copy"] is True
    assert r2.payload["copied_bytes"] == 0
    assert r2.payload["length"] == 3
    assert r2.payload["null_count"] == 1

    r3 = service.concat_arrays(
        [r1.payload["handle"], r2.payload["handle"]], None, run_id=run_id
    )
    assert r3.status == "committed"
    assert r3.payload["values"] == [0, None, 2, 3, None, 2, 3]
    assert r3.payload["copy"]["copied_bytes"] > 0

    ops = store.list_operations(run_id)
    statuses = [(o["name"], o["status"]) for o in ops]
    assert statuses == [
        ("import", "committed"),
        ("slice", "committed"),
        ("concat", "committed"),
    ]
    test_log.step(run_id, "assert", "committed", ops=len(ops))


def test_decreasing_offsets_rejected_not_failed_and_no_array_registered(
    service: ViewService, store, run_id: str
):
    import base64
    import struct

    desc = {
        "format": "raw_buffers",
        "type": "utf8",
        "length": 3,
        "buffers": [
            None,
            base64.b64encode(struct.pack("<iiii", 0, 3, 2, 5)).decode(),
            base64.b64encode(b"abcde").decode(),
        ],
    }
    result = service.import_array(desc, run_id=run_id)
    assert result.status == "rejected"
    assert result.error["code"] == "VALIDATION"
    codes = [v["code"] for v in result.error["violations"]]
    assert codes == ["DECREASING_OFFSET"]
    # rejected imports must not leak into the registry/metadata
    assert store.list_arrays(run_id) == []
    ops = store.list_operations(run_id)
    assert ops[0]["status"] == "rejected"
    assert ops[0]["error_code"] == "VALIDATION"


def test_cross_type_concat_rejected_with_category(
    service: ViewService, store, run_id: str
):
    r1 = _import(service, run_id, "int32", [1, 2])
    r2 = _import(service, run_id, "int64", [3, 4])
    result = service.concat_arrays(
        [r1.payload["handle"], r2.payload["handle"]], None, run_id=run_id
    )
    assert result.status == "rejected"
    assert result.error["code"] == "TYPE_MISMATCH"
    assert result.error["input_types"] == ["int32", "int64"]
    # explicit cast then succeeds
    ok = service.concat_arrays(
        [r1.payload["handle"], r2.payload["handle"]], "int64", run_id=run_id
    )
    assert ok.status == "committed"
    assert ok.payload["values"] == [1, 2, 3, 4]


def test_slice_out_of_range_rejected_with_category(
    service: ViewService, run_id: str
):
    r1 = _import(service, run_id, "int32", [1, 2])
    result = service.slice_array(r1.payload["handle"], 1, 5, run_id=run_id)
    assert result.status == "rejected"
    assert result.error["code"] == "SLICE_RANGE"


def test_unknown_handle_is_not_found_not_generic_success(
    service: ViewService, run_id: str
):
    result = service.get_values("arr_does_not_exist", run_id=run_id)
    assert result.status == "rejected"
    assert result.error["code"] == "NOT_FOUND"


def test_unknown_format_rejected(service: ViewService, run_id: str):
    result = service.import_array(
        {"format": "parquet", "type": "int32", "values": [1]}, run_id=run_id
    )
    assert result.status == "rejected"
    assert result.error["code"] == "FORMAT"


def test_validate_endpoint_returns_specific_codes(service: ViewService, run_id: str):
    import base64
    import struct

    desc = {
        "type": "utf8",
        "length": 3,
        "offset": 0,
        "buffers": [
            None,
            base64.b64encode(struct.pack("<iiii", 0, 9, 8, 20)).decode(),
            base64.b64encode(b"abc").decode(),
        ],
    }
    result = service.validate_descriptor(desc, run_id=run_id)
    payload = result.payload
    assert payload["accepted"] is False
    assert "DECREASING_OFFSET" in payload["violation_codes"]
    assert "OFFSET_OUT_OF_BOUNDS" in payload["violation_codes"]
    layers = {v["layer"] for v in payload["violations"]}
    assert "offsets" in layers and "data" in layers


def test_run_id_correlates_all_operations(service: ViewService, store):
    run_id = make_run_id("correlate")
    _import(service, run_id, "utf8", ["x", None])
    _import(service, run_id, "utf8", ["y"])
    ops = store.list_operations(run_id)
    assert len(ops) == 2
    assert all(o["run_id"] == run_id for o in ops)
    run = store.get_run(run_id)
    assert "pyarrow" in run["versions"]
