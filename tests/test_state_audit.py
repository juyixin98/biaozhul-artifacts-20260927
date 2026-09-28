"""State isolation and fingerprint-only audit tests."""
from __future__ import annotations

import json

from app.core.envelope import fingerprint
from app.core.shamir import RecoverStatus
from conftest import SECRET_A, make_collection


def test_collections_are_isolated_in_storage(kernel, store):
    make_collection(kernel, SECRET_A, 2, 3, request_id="r1", cid="iso_a")
    make_collection(kernel, b"another secret!!!", 2, 3, request_id="r2", cid="iso_b")

    rows_a = store.list_shares("iso_a")
    rows_b = store.list_shares("iso_b")
    assert {r["x"] for r in rows_a} == {1, 2, 3}
    assert {r["x"] for r in rows_b} == {1, 2, 3}
    # same x across collections must hold different y (independent random polys)
    ya = json.loads(rows_a[0]["ys"])
    yb = json.loads(rows_b[0]["ys"])
    assert ya != yb
    # collection row carries its own key and params
    ca = store.get_collection("iso_a")
    cb = store.get_collection("iso_b")
    assert ca["mac_key"] != cb["mac_key"]
    assert ca["collection_id"] != cb["collection_id"]


def test_recovery_is_scoped_to_collection(kernel):
    a = make_collection(kernel, SECRET_A, 2, 3, request_id="r", cid="sc_a")
    b = make_collection(kernel, b"bbbbbbbbbbbbbbbb", 2, 3, request_id="r", cid="sc_b")
    # cross-using shares cannot unlock the other collection
    report = kernel.recover(
        request_id="req_cross", collection_id="sc_a",
        submitted=[b["shares"][0], b["shares"][1]],
    )
    assert report.status == RecoverStatus.REJECTED_INSUFFICIENT
    assert all(r["reason"] == "wrong_collection" for r in report.rejected)


def test_audit_events_are_fingerprint_only(kernel, store, capsys):
    out = make_collection(kernel, SECRET_A, 3, 5, request_id="req_aud", cid="aud_c")
    kernel.recover(
        request_id="req_aud_rec", collection_id="aud_c",
        submitted=out["shares"][:3],
    )
    events = store.query_audit(request_id="req_aud_rec")
    assert events, "expected an audit event for recovery"
    event = events[0]
    assert event["verdict"] == RecoverStatus.RECOVERED_UNVERIFIABLE.value
    fps = json.loads(event["fingerprints"])
    assert len(fps) == 3
    assert all(fp.startswith("sha256:") for fp in fps)

    # No secret material anywhere in the persisted event.
    blob = json.dumps(event)
    assert SECRET_A.decode() not in blob
    for share in out["shares"][:3]:
        for y in share["ys"]:
            assert str(y) not in blob
        assert share["mac"] not in blob

    # Each stored y must not appear in any audit row either.
    all_events = json.dumps(store.query_audit(limit=100))
    for share in out["shares"]:
        for y in share["ys"]:
            assert str(y) not in all_events


def test_audit_carries_request_and_collection_id(kernel, store):
    make_collection(kernel, SECRET_A, 2, 3, request_id="req_zz", cid="cid_zz")
    ev = store.query_audit(request_id="req_zz")
    assert ev and ev[0]["collection_id"] == "cid_zz"
    by_coll = store.query_audit(collection_id="cid_zz")
    assert by_coll and by_coll[0]["request_id"] == "req_zz"


def test_diagnostic_explains_rejection(kernel):
    out = make_collection(kernel, SECRET_A, 3, 5, request_id="rd", cid="diag")
    report = kernel.recover(
        request_id="req_diag", collection_id="diag", submitted=out["shares"][:1]
    )
    assert report.status == RecoverStatus.REJECTED_INSUFFICIENT
    assert report.math is None
    # rejected list empty (the one share is fine) but count < threshold explains
    assert report.distinct_xs == [1]
    audit = kernel.auditor.query(request_id="req_diag")
    assert "threshold" in audit[0]["detail"]
