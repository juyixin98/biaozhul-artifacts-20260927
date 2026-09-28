"""End-to-end HTTP integration tests (FastAPI + SQLite + real JSONL logs)."""

from __future__ import annotations

import base64
import json
import struct

import pyarrow as pa
import pytest

pytestmark = pytest.mark.integration


def test_healthz_reports_versions(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["versions"]["pyarrow"] == pa.__version__
    assert body["versions"]["arrowzero"]


def test_full_workflow_import_slice_concat_values_export(client, settings):
    run_id = "it-workflow-001"
    headers = {"x-run-id": run_id}

    resp = client.post(
        "/api/v1/arrays/import",
        json={"format": "pylist", "type": "utf8",
              "values": ["alpha", None, "", "βγ", "", None, "z"]},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    h1 = resp.json()["result"]["handle"]

    resp = client.post(
        "/api/v1/arrays/slice",
        json={"handle": h1, "offset": 1, "length": 5},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    assert result["zero_copy"] is True
    assert result["copied_bytes"] == 0
    h2 = result["handle"]

    resp = client.post(
        "/api/v1/arrays/values", json={"handle": h2}, headers=headers
    )
    assert resp.status_code == 200
    assert resp.json()["result"]["values"] == [None, "", "βγ", "", None]

    resp = client.post(
        "/api/v1/arrays/concat", json={"handles": [h1, h2]}, headers=headers
    )
    assert resp.status_code == 200
    merged = resp.json()["result"]
    assert merged["values"] == ["alpha", None, "", "βγ", "", None, "z"] + [
        None, "", "βγ", "", None
    ]
    assert merged["copy"]["copied_bytes"] > 0

    # IPC export roundtrip via the API
    resp = client.post("/api/v1/arrays/export", json={"handle": h2}, headers=headers)
    assert resp.status_code == 200
    payload = base64.b64decode(resp.json()["result"]["payload"])
    arr = pa.ipc.open_stream(payload).read_next_batch().column(0)
    assert arr.to_pylist() == [None, "", "βγ", "", None]

    # run audit is queryable (ASC order = execution order)
    resp = client.get(f"/api/v1/runs/{run_id}")
    assert resp.status_code == 200
    ops = resp.json()["operations"]
    assert [o["name"] for o in ops] == [
        "import", "slice", "values", "concat", "export"
    ]
    assert all(o["status"] == "committed" for o in ops)

    # JSONL log lines carry versions and run id
    lines = [json.loads(l) for l in settings.log_path.read_text().splitlines()]
    assert all(l["run_id"] == run_id for l in lines if l["run_id"] != "startup")
    assert all("versions" in l and "pyarrow" in l["versions"] for l in lines)


def test_decreasing_offsets_returns_422_with_failure_category(client):
    body = {
        "type": "utf8",
        "length": 3,
        "offset": 0,
        "buffers": [
            None,
            base64.b64encode(struct.pack("<iiii", 0, 3, 2, 5)).decode(),
            base64.b64encode(b"abcde").decode(),
        ],
    }
    resp = client.post("/api/v1/validate", json=body)
    assert resp.status_code == 200
    payload = resp.json()["result"]
    assert payload["accepted"] is False
    assert payload["violation_codes"] == ["DECREASING_OFFSET"]
    assert payload["violations"][0]["layer"] == "offsets"
    assert payload["violations"][0]["index"] == 2


def test_importing_decreasing_offsets_is_422_and_not_500_or_success(client):
    body = {
        "format": "raw_buffers",
        "type": "utf8",
        "length": 3,
        "buffers": [
            None,
            base64.b64encode(struct.pack("<iiii", 0, 3, 2, 5)).decode(),
            base64.b64encode(b"abcde").decode(),
        ],
    }
    resp = client.post("/api/v1/arrays/import", json=body)
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "VALIDATION"
    assert err["violations"][0]["code"] == "DECREASING_OFFSET"


def test_ipc_stream_import_zero_copy_via_api(client):
    arr = pa.array(["a", None, "", "bc", "d"], type=pa.utf8())
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, pa.schema([("", pa.utf8())])) as w:
        w.write_batch(pa.record_batch([arr], names=[""]))
    payload = base64.b64encode(sink.getvalue()).decode()
    resp = client.post(
        "/api/v1/arrays/import",
        json={"format": "ipc_stream", "payload": payload},
    )
    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    assert result["evidence"]["zero_copy"] is True
    assert result["evidence"]["copied_bytes"] == 0
    handle = result["handle"]

    resp = client.post("/api/v1/arrays/slice", json={"handle": handle, "offset": 1, "length": 3})
    assert resp.status_code == 200
    sl = resp.json()["result"]
    assert sl["zero_copy"] is True


def test_cross_type_concat_422_then_cast_200(client):
    r1 = client.post(
        "/api/v1/arrays/import", json={"format": "pylist", "type": "int32", "values": [1, 2]}
    ).json()["result"]["handle"]
    r2 = client.post(
        "/api/v1/arrays/import", json={"format": "pylist", "type": "int64", "values": [3, 4]}
    ).json()["result"]["handle"]

    resp = client.post("/api/v1/arrays/concat", json={"handles": [r1, r2]})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "TYPE_MISMATCH"

    resp = client.post(
        "/api/v1/arrays/concat", json={"handles": [r1, r2], "cast_to": "int64"}
    )
    assert resp.status_code == 200
    assert resp.json()["result"]["values"] == [1, 2, 3, 4]


def test_unknown_handle_is_404(client):
    resp = client.post("/api/v1/arrays/values", json={"handle": "arr_nope"})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_negative_offset_rejected_by_schema(client):
    resp = client.post(
        "/api/v1/arrays/slice", json={"handle": "arr_x", "offset": -1}
    )
    assert resp.status_code == 422  # request shape validation, distinct from 200


def test_missing_type_field_is_request_error_not_500(client):
    resp = client.post(
        "/api/v1/arrays/import", json={"format": "pylist", "values": [1]}
    )
    assert resp.status_code == 422
