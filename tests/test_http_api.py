"""HTTP-level tests: concrete outputs, concrete failure categories."""
from __future__ import annotations

from . import fixtures
from .conftest import assert_error
from .oracle import expected_encoding


def test_encode_json_concrete_result_and_roundtrip(client, make_payload):
    refs = fixtures.repeated_dict_items()
    resp = client.post("/v1/encode", json=make_payload(refs))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is True
    assert data["run_id"].startswith("run-")

    gd = [(e["type"], e["value"]) for e in data["global_dictionary"]["entries"]]
    assert gd == [("utf8", "a"), ("utf8", "b"), ("utf8", "z")]
    assert data["global_dictionary"]["cardinality"] == 3
    assert data["policy"]["global_index_width"] == 8

    b1 = data["batches"][0]
    assert b1["local_to_global"] == [0, 1, 0]
    assert b1["global_indices"] == [0, 1, 0, 0, None]
    assert b1["valid"] == [True, True, True, True, False]
    b2 = data["batches"][1]
    assert b2["local_to_global"] == [2, 1]
    assert b2["global_indices"] == [2, 1, 2]

    # Encode-time verification reports a concrete pass.
    assert data["verification"]["all_match"] is True
    assert data["verification"]["checked_rows"] == 8
    assert data["verification"]["mismatches"] == []

    # Kernel result agrees with the independent oracle.
    ref = expected_encoding(refs)
    assert b2["local_to_global"] == list(ref.local_to_global["b2"])


def test_empty_and_all_null_over_http(client, make_payload):
    refs = fixtures.empty_and_nulls()
    resp = client.post("/v1/encode", json=make_payload(refs))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["global_dictionary"]["entries"] == []
    assert data["global_dictionary"]["cardinality"] == 0
    all_null = data["batches"][1]
    assert all_null["global_indices"] == [None, None, None]
    assert all_null["valid"] == [False, False, False]
    assert all_null["stats"]["null_rows"] == 3


def test_repeated_dict_items_default_merge_vs_strict(client, make_payload):
    refs = fixtures.repeated_dict_items()
    # Default: merge succeeds and the repeated item is counted.
    ok = client.post("/v1/encode", json=make_payload(refs))
    assert ok.status_code == 200
    assert ok.json()["batches"][0]["stats"]["duplicate_declared"] == 1

    # Strict: the same input is a classified failure, not a 200.
    strict = client.post("/v1/encode",
                         json=make_payload(refs, on_duplicate_values="error"))
    assert_error(strict, 422, "DUPLICATE_DICTIONARY_VALUE")


def test_overflow_reject_and_expand_concrete_widths(client, make_payload):
    refs = fixtures.overflow_257()
    rejected = client.post("/v1/encode",
                           json=make_payload(refs, target_width=8))
    body = assert_error(rejected, 422, "CARDINALITY_OVERFLOW")
    assert body["error"]["details"]["required_width"] == 16
    assert body["error"]["details"]["target_capacity"] == 256

    expanded = client.post("/v1/encode", json=make_payload(
        refs, target_width=8, width_policy="expand"))
    assert expanded.status_code == 200, expanded.text
    assert expanded.json()["policy"]["global_index_width"] == 16


def test_dictionary_null_is_a_classified_error(client, make_payload):
    payload = {"batches": [{
        "batch_id": "bad", "dictionary": ["a", None],
        "indices": [0], "valid": [True]}]}
    resp = client.post("/v1/encode", json=payload)
    body = assert_error(resp, 422, "DICTIONARY_CONTAINS_NULL")
    assert body["error"]["batch_id"] == "bad"
    assert body["error"]["details"]["local_code"] == 1


def test_out_of_range_index_on_valid_row(client, make_payload):
    payload = {"batches": [{
        "batch_id": "oob", "dictionary": ["a"],
        "indices": [7], "valid": [True]}]}
    body = assert_error(client.post("/v1/encode", json=payload),
                        422, "INDEX_OUT_OF_RANGE")
    assert body["error"]["details"]["row"] == 0


def test_malformed_shapes_never_return_success(client):
    cases = [
        ({}, "batches missing"),
        ({"batches": []}, "empty batch list"),
        ({"batches": [{"dictionary": [], "indices": []}]}, "no batch id"),
        ({"batches": [{"batch_id": "x", "dictionary": "a",
                       "indices": []}]}, "dict not list"),
        ({"batches": [{"batch_id": "x", "dictionary": [],
                       "indices": [0]}]}, "valid index into empty dict"),
        ({"batches": [{"batch_id": "x", "dictionary": [], "indices": [],
                       "valid": [True]}]}, "valid length mismatch"),
        ({"batches": [{"batch_id": "x", "dictionary": [1.5],
                       "indices": []}]}, "float value unsupported"),
        ({"batches": [{"batch_id": "x", "dictionary": [],
                       "indices": []}], "target_width": 3}, "bad width"),
    ]
    for payload, label in cases:
        resp = client.post("/v1/encode", json=payload)
        assert resp.status_code in (400, 422), (label, resp.status_code,
                                                resp.text)
        assert resp.json()["ok"] is False, label


def test_duplicate_batch_id_rejected(client):
    payload = {"batches": [
        {"batch_id": "same", "dictionary": ["a"], "indices": [0],
         "valid": [True]},
        {"batch_id": "same", "dictionary": ["b"], "indices": [0],
         "valid": [True]}]}
    assert_error(client.post("/v1/encode", json=payload),
                 422, "DUPLICATE_BATCH_ID")


def test_run_id_conflict_and_get_run(client, make_payload):
    refs = fixtures.empty_and_nulls()
    payload = make_payload(refs, run_id="fixed-id")
    assert client.post("/v1/encode", json=payload).status_code == 200
    assert_error(client.post("/v1/encode", json=payload),
                 409, "RUN_CONFLICT")

    got = client.get("/v1/runs/fixed-id")
    assert got.status_code == 200
    run = got.json()["run"]
    assert run["status"] == "ok"
    assert run["cardinality"] == 0
    assert {b["batch_id"] for b in run["batches"]} == {"empty", "all_null"}


def test_unknown_run_is_404_not_success(client):
    assert_error(client.get("/v1/runs/nope"), 404, "RUN_NOT_FOUND")


def test_healthz_and_version(client):
    h = client.get("/healthz")
    assert h.status_code == 200 and h.json()["ok"] is True
    v = client.get("/version")
    assert v.status_code == 200
    info = v.json()
    assert "pyarrow" in info and "fastapi" in info and "sqlite" in info
