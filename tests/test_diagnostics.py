"""Diagnostics tests: request ids, decisions, and the redaction contract."""
from __future__ import annotations

import json
import logging

from tests.helpers import b64, create_version, feed, open_scan


SECRET_MARKER = b"SECRET-pattern-xyz"
SECRET_TEXT = "SECRET-payload-xyz"


def test_diagnostics_explain_accept_with_key_state(client):
    v = create_version(client, [b"abc", b"bc"])
    sid = open_scan(client, v)
    feed(client, sid, b"abc", )
    # Pull events by the client-supplied request id of the feed.
    rid = "diag-accept-0001"
    resp = client.post(
        f"/scans/{sid}/chunks", json={"chunk": b64(b"bc")},
        headers={"X-Request-ID": rid},
    )
    assert resp.status_code == 200
    events = client.get(f"/diagnostics/requests/{rid}").json()
    kinds = {e["kind"]: e for e in events}
    assert "request" in kinds and "decision" in kinds
    decision = kinds["decision"]
    assert decision["decision"] == "accept"
    assert decision["code"] == "chunk_accepted"
    state = json.loads(decision["state_json"])
    # Stream is "abc"+"bc" = "abcbc": it ends mid-trie on the "bc" suffix
    # node (not root), and 5 bytes have been consumed.
    assert state["node"] != 0
    assert state["bytes_consumed"] == 5
    assert state["new_hits"] >= 1
    # Pattern reference is id+length, never content.
    for ref in state.get("hit_sample_patterns", []):
        assert ref.startswith("pat#")
        assert "bytes" in ref


def test_diagnostics_explain_rejection_category(client, caplog):
    caplog.set_level(logging.INFO, logger="ac.diagnostics")
    # Empty pattern -> reject.
    resp = client.post("/versions", json={
        "encoding": "binary", "case_mode": "sensitive",
        "patterns": [b64(SECRET_MARKER), b64(b"")],
    })
    rid = resp.headers["x-request-id"]
    assert resp.status_code == 422
    body = resp.json()["error"]
    assert body["request_id"] == rid
    assert body["decision"] == "reject"

    events = client.get(f"/diagnostics/requests/{rid}").json()
    decisions = [e for e in events if e["kind"] == "decision"]
    assert decisions and decisions[0]["decision"] == "reject"
    assert decisions[0]["code"] == "empty_pattern"


def test_raw_pattern_bytes_never_persisted(client):
    resp = client.post("/versions", json={
        "encoding": "binary", "case_mode": "sensitive",
        "patterns": [b64(SECRET_MARKER)],
    })
    assert resp.status_code == 201
    rows = client.get("/diagnostics/events?limit=1000").json()
    blob = json.dumps(rows)
    # The diagnostic tables must contain no pattern content...
    assert SECRET_MARKER.decode() not in blob
    assert SECRET_MARKER.hex() not in blob
    # ...while counts do appear.
    assert any(e["code"] == "version_created" for e in rows)


def test_raw_chunk_bytes_never_persisted(client):
    v = create_version(client, [b"needle"])
    sid = open_scan(client, v)
    feed(client, sid, SECRET_TEXT.encode())
    rows = client.get("/diagnostics/events?limit=1000").json()
    blob = json.dumps(rows)
    assert SECRET_TEXT not in blob
    # Length is recorded though.
    chunk_events = [e for e in rows if e["code"] == "chunk_accepted"]
    assert chunk_events


def test_redaction_helper_does_not_leak():
    from app.diagnostics import redact_bytes
    out = redact_bytes(SECRET_MARKER)
    assert str(len(SECRET_MARKER)) in out
    assert "SECRET" not in out


def test_internal_error_is_recorded_inconclusive(client, container,
                                                 monkeypatch):
    # Force an unexpected failure below the domain layer; the boundary must
    # classify it "inconclusive" (it cannot promise a result) and must not
    # leak the exception text to the client.
    def boom(*_a, **_k):
        raise RuntimeError("SENSITIVE internal detail boom")

    monkeypatchattr = monkeypatch
    monkeypatchattr.setattr(container.scans, "status", boom)

    resp = client.get("/scans/anything")
    assert resp.status_code == 500
    err = resp.json()["error"]
    assert err["decision"] == "inconclusive"
    assert err["code"] == "internal_error"
    assert "SENSITIVE" not in json.dumps(err)
    rid = err["request_id"]
    events = client.get(f"/diagnostics/requests/{rid}").json()
    decisions = [e for e in events if e["kind"] == "decision"]
    assert any(e["decision"] == "inconclusive" for e in decisions)
    assert "SENSITIVE" not in json.dumps(events)


def test_404_records_reject_decision(client):
    resp = client.get("/scans/does-not-exist")
    assert resp.status_code == 404
    rid = resp.headers["x-request-id"]
    events = client.get(f"/diagnostics/requests/{rid}").json()
    decisions = [e for e in events if e["kind"] == "decision"]
    assert len(decisions) == 1
    assert decisions[0]["decision"] == "reject"
    assert decisions[0]["code"] == "scan_not_found"
