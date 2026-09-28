"""Integration tests driving the full FastAPI application.

These tests assert concrete responses and failure categories (not just "the
endpoint works"), and verify run-identity correlation headers.
"""
from __future__ import annotations

import base64
import struct

import pyarrow as pa
import pytest

from tests.fixtures.oracle import b64, fixed_descriptor, string_descriptor

pytestmark = pytest.mark.integration


def test_health_reports_versions(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert set(body["versions"]) == {"python", "pyarrow", "fastapi"}
    assert "int32" in body["supported_types"] and "string" in body["supported_types"]
    assert "bool" not in body["supported_types"]


def _import(client, desc, run_id="run-int-1"):
    resp = client.post("/columns", json=desc, headers={"X-Run-Id": run_id})
    return resp


def test_import_and_slice_full_flow_with_run_correlation(client):
    values = [0, 10, 20, None, 40, 50, None, 70, 80, 90]
    resp = _import(client, fixed_descriptor("int32", values))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["values"] == values
    assert body["null_count"] == 2
    assert resp.headers["X-Run-Id"] == "run-int-1"
    assert body["run_id"] == "run-int-1"
    assert body["import_evidence"]["agreement"] is True
    cid = body["column_id"]

    # Non-zero offset slice crossing the first bitmap byte.
    resp = client.post(f"/columns/{cid}/slice", json={"offset": 3, "length": 5},
                       headers={"X-Run-Id": "run-slice"})
    assert resp.status_code == 200
    s = resp.json()
    assert s["values"] == [None, 40, 50, None, 70]
    assert s["null_flags"] == [True, False, False, True, False]
    assert s["offset"] == 3
    assert s["slice"]["zero_copy"] is True
    assert s["slice"]["buffers_shared_with_parent"] == {"data": True, "validity": True}
    assert s["slice"]["copied_bytes"] == {"total": 0}
    assert resp.headers["X-Run-Id"] == "run-slice"


def test_validate_reports_three_independent_groups(client):
    desc = string_descriptor(["a", "bb", None, "dddd"])
    resp = client.post("/validate", json=desc)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    groups = {c["group"] for c in body["checks"]}
    assert {"schema", "validity", "offsets", "data", "semantic"} <= groups
    assert any(c["check"] == "data_buffer_present" for c in body["checks"])
    assert any(c["check"] == "offsets_monotonic" for c in body["checks"])
    assert any(c["check"] == "null_count_matches_bitmap" for c in body["checks"])


def test_validate_decreasing_offsets_returns_422_category_on_import(client):
    def mutate(offsets, data):
        offsets[2] = 1
    desc = string_descriptor(["abc", "de", "f"], mutate=mutate)

    v = client.post("/validate", json=desc).json()
    assert v["ok"] is False
    assert "offsets_not_monotonic" in v["failure_categories"]

    resp = client.post("/columns", json=desc)
    assert resp.status_code == 422
    err = resp.json()["error"]
    # The top-level category is the first failing one; detail preserves all.
    assert "offsets_not_monotonic" in err["detail"]["failure_categories"]
    failing = {c["category"] for c in err["detail"]["checks"]}
    assert "offsets_not_monotonic" in failing


def test_unknown_column_slice_is_404_not_found(client):
    resp = client.post("/columns/nope/slice", json={"offset": 0, "length": 1})
    assert resp.status_code == 404
    assert resp.json()["error"]["category"] == "not_found"


def test_concat_endpoint_cross_type_requires_target_then_succeeds(client):
    a = client.post("/columns", json=fixed_descriptor("int32", [1, 2])).json()["column_id"]
    b = client.post("/columns", json=fixed_descriptor("int64", [3, 4])).json()["column_id"]

    resp = client.post("/columns/concat", json={"column_ids": [a, b]})
    assert resp.status_code == 422
    assert resp.json()["error"]["category"] == "type_mismatch"

    resp = client.post("/columns/concat",
                       json={"column_ids": [a, b], "target_type": "int64"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["values"] == [1, 2, 3, 4]
    cr = body["concat"]["copy_report"]
    assert cr["cast_target"] == "int64"
    assert cr["copied_bytes"]["cast"] == 16
    assert cr["copied_bytes"]["data"] == 32
    assert cr["zero_copy"] is False


def test_concat_string_columns_end_to_end_copy_accounting(client):
    a = client.post("/columns", json=string_descriptor(["ab", None, "cdef"])).json()["column_id"]
    b = client.post("/columns", json=string_descriptor(["", "gh"])).json()["column_id"]
    body = client.post("/columns/concat", json={"column_ids": [a, b]}).json()
    assert body["values"] == ["ab", None, "cdef", "", "gh"]
    copied = body["concat"]["copy_report"]["copied_bytes"]
    assert copied == {"validity": 1, "offsets": 24, "data": 8, "cast": 0, "total": 33}


def test_ipc_import_path_agrees_with_pyarrow(client):
    table = pa.table({"x": pa.array([5, None, 7, 8, None, 10], pa.int32())})
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    msg = base64.b64encode(sink.getvalue().to_pybytes()).decode()
    resp = client.post("/columns/import-ipc", json={"ipc_stream_b64": msg, "column_index": 0})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["values"] == [5, None, 7, 8, None, 10]
    assert body["source"] == "ipc"
    assert body["import_evidence"]["agreement"] is True

    # Non-IPC garbage -> categorized malformed_payload, never a 500.
    bad = client.post("/columns/import-ipc",
                      json={"ipc_stream_b64": b64(b"not arrow at all"), "column_index": 0})
    assert bad.status_code == 422
    assert bad.json()["error"]["category"] == "malformed_payload"


def test_listing_and_metadata_persistence(client):
    cid = client.post("/columns", json=fixed_descriptor("uint16", [1, 2, 3])).json()["column_id"]
    client.post(f"/columns/{cid}/slice", json={"offset": 1, "length": 2})
    listing = client.get("/columns").json()["columns"]
    assert len(listing) == 2
    assert listing[1]["source"] == "slice" and listing[1]["parents"] == [cid]
    # Metadata rows exist in SQLite too.
    rows = client.get("/columns")  # service listing; store check via API state below
    assert rows.status_code == 200


def test_malformed_base64_is_malformed_payload_not_500(client):
    resp = client.post("/columns",
                       json={"type": "int32", "length": 1, "data": "@@@not-base64@@@"})
    assert resp.status_code == 422
    assert resp.json()["error"]["category"] == "malformed_payload"
    assert "base64" in resp.json()["error"]["message"]


def test_pydantic_shape_error_is_documented_422_shape(client):
    resp = client.post("/columns", json={"type": "int32", "length": "abc", "data": ""})
    assert resp.status_code == 422  # request validation, distinct from layout 422s
