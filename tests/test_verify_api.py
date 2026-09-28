"""/v1/verify tests: independent re-decode from stored metadata."""
from __future__ import annotations

from . import fixtures
from .conftest import assert_error
from .oracle import expected_decoded_rows


def _encode(client, make_payload, refs, run_id="v1"):
    resp = client.post("/v1/encode", json=make_payload(refs, run_id=run_id))
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_verify_accepts_rows_independently_redecoded_from_store(
        client, make_payload):
    refs = fixtures.repeated_dict_items()
    _encode(client, make_payload, refs)
    rows = expected_decoded_rows(refs)  # computed by the independent oracle
    body = {"run_id": "v1", "batches": [
        {"batch_id": bid, "rows": list(vals)}
        for bid, vals in rows.items()]}
    resp = client.post("/v1/verify", json=body)
    assert resp.status_code == 200, resp.text
    verdict = resp.json()["verification"]
    assert verdict["all_match"] is True
    assert verdict["checked_rows"] == 8
    assert verdict["mismatches"] == []


def test_verify_flags_concrete_tampered_value_and_null(client, make_payload):
    refs = fixtures.repeated_dict_items()
    _encode(client, make_payload, refs)
    rows = {bid: list(vals) for bid, vals in
            expected_decoded_rows(refs).items()}
    # Tamper: b1 row1 b->z (wrong value); row4 null -> 'a' (wrong nullness).
    rows["b1"][1] = "z"
    rows["b1"][4] = "a"
    resp = client.post("/v1/verify", json={"run_id": "v1", "batches": [
        {"batch_id": bid, "rows": vals}
        for bid, vals in rows.items()]})
    verdict = resp.json()["verification"]
    assert verdict["all_match"] is False
    assert {(m["batch_id"], m["row"], m["reason"]) for m in
            verdict["mismatches"]} == {
        ("b1", 1, "VALUE_MISMATCH"),
        ("b1", 4, "VALUE_MISMATCH")}
    bad1 = next(m for m in verdict["mismatches"] if m["row"] == 1)
    assert bad1["expected"] == "b" and bad1["candidate"] == "z"


def test_verify_reports_missing_and_unknown_batches(client, make_payload):
    refs = fixtures.repeated_dict_items()
    _encode(client, make_payload, refs)
    body = {"run_id": "v1", "batches": [
        {"batch_id": "b1", "rows": ["a", "b", "a", "a", None]},
        {"batch_id": "ghost", "rows": []}]}
    verdict = client.post("/v1/verify", json=body).json()["verification"]
    assert verdict["all_match"] is False
    reasons = {(m["batch_id"], m["reason"]) for m in verdict["mismatches"]}
    assert ("b2", "MISSING_BATCH") in reasons
    assert ("ghost", "UNKNOWN_BATCH") in reasons


def test_verify_reports_row_count_mismatch(client, make_payload):
    refs = fixtures.repeated_dict_items()
    _encode(client, make_payload, refs)
    body = {"run_id": "v1", "batches": [
        {"batch_id": "b1", "rows": ["a"]},
        {"batch_id": "b2", "rows": ["z", "b", "z"]}]}
    mismatches = client.post("/v1/verify", json=body).json()[
        "verification"]["mismatches"]
    assert (mismatches[0]["reason"] == "ROW_COUNT"
            and mismatches[0]["expected"] == 5
            and mismatches[0]["actual"] == 1)


def test_verify_unknown_run_is_404(client):
    assert_error(client.post("/v1/verify",
                             json={"run_id": "nope", "batches": []}),
                 404, "RUN_NOT_FOUND")


def test_http_order_invariance_of_remap(client, make_payload):
    refs = fixtures.permutation_triplet()
    r1 = client.post("/v1/encode",
                     json=make_payload(refs, run_id="ord-a")).json()
    r2 = client.post("/v1/encode",
                     json=make_payload([refs[1], refs[0], refs[2]],
                                       run_id="ord-b")).json()
    assert [tuple(e.items()) for e in r1["global_dictionary"]["entries"]] == \
           [tuple(e.items()) for e in r2["global_dictionary"]["entries"]]
    for bid in ("p1", "p2", "p3"):
        a = next(b for b in r1["batches"] if b["batch_id"] == bid)
        b = next(x for x in r2["batches"] if x["batch_id"] == bid)
        assert a["local_to_global"] == b["local_to_global"]
        assert a["global_indices"] == b["global_indices"]
