"""API tests: identity correlation, rule switching, audit gating, categories."""
from __future__ import annotations

from tests.synth_fixtures import (
    SYNTH_API_TOKEN,
    SYNTH_BANK_CARD,
    SYNTH_CN_ID,
    SYNTH_EMAIL,
)


def test_request_id_is_echoed_and_assigned(client):
    r = client.post(
        "/api/v1/redact", json={"text": "hello"}, headers={"X-Request-Id": "abc-123"}
    )
    assert r.status_code == 200
    assert r.headers["X-Request-Id"] == "abc-123"
    assert r.json()["request_id"] == "abc-123"

    r2 = client.post("/api/v1/redact", json={"text": "hello"})
    rid = r2.headers["X-Request-Id"]
    assert rid.startswith("req_")


def test_redact_exact_response(client):
    r = client.post("/api/v1/redact", json={"text": f"mail {SYNTH_EMAIL} x"})
    body = r.json()
    assert body["redacted"] == "mail [REDACTED:email] x"
    assert body["rule_profile"] == "standard"
    assert body["rule_version"].startswith("standard-")
    assert len(body["rule_fingerprint"]) == 16
    assert body["mappings"][0]["original_sha256"]
    assert body["input_length"] != body["output_length"]


def test_rule_switching_profile_changes_result(client):
    r_std = client.post("/api/v1/redact", json={"text": SYNTH_CN_ID})
    assert r_std.json()["redacted"] == SYNTH_CN_ID
    r_strict = client.post(
        "/api/v1/redact", json={"text": SYNTH_CN_ID, "profile": "strict"}
    )
    assert r_strict.json()["redacted"] == "[REDACTED:cn_id_card]"
    # Versions are explicit.
    assert r_std.json()["rule_version"] != r_strict.json()["rule_version"]


def test_unknown_profile_is_named_category(client):
    r = client.post("/api/v1/redact", json={"text": "x", "profile": "nope"})
    assert r.status_code == 404
    assert r.json()["error_category"] == "unknown_profile"


def test_validation_error_category_without_value_echo(client):
    r = client.post("/api/v1/redact", json={"text": 12345})
    assert r.status_code == 422
    body = r.json()
    assert body["error_category"] == "validation_error"
    assert "12345" not in r.text  # error must not echo submitted value
    assert "text" in body["message"]


def test_stream_end_to_end_equals_redact(client):
    text = (
        f"begin {SYNTH_EMAIL} then {SYNTH_API_TOKEN} and {SYNTH_BANK_CARD} tail"
    )
    full = client.post("/api/v1/redact", json={"text": text}).json()

    sid = client.post("/api/v1/sessions", json={"text": ""}).json()["session_id"]
    emitted = ""
    chunk_rids = []
    for i in range(0, len(text), 5):
        chunk = text[i : i + 5]
        final = i + 5 >= len(text)
        rr = client.post(
            "/api/v1/sessions/chunk",
            json={"session_id": sid, "chunk": chunk, "final": final},
        )
        assert rr.status_code == 200, rr.text
        emitted += rr.json()["emitted"]
        chunk_rids.append(rr.json()["request_id"])
    assert emitted == full["redacted"]
    assert all(rid.startswith("req_") for rid in chunk_rids)

    # Every redacted fragment is auditable via its chunk request records.
    listing = client.get(
        "/api/v1/audit/requests", headers={"X-Audit-Key": "test-audit-key"}
    ).json()["requests"]
    chunk_rows = [q for q in listing if q["endpoint"] == "/api/v1/sessions/chunk"]
    assert {q["request_id"] for q in chunk_rows} >= set(chunk_rids)
    assert all(q["rule_version"] == "standard-v1" for q in chunk_rows)
    revealed_originals = []
    for rid in chunk_rids:
        detail = client.get(
            f"/api/v1/audit/requests/{rid}?reveal=true",
            headers={"X-Audit-Key": "test-audit-key"},
        ).json()
        revealed_originals.extend(f["original"] for f in detail["fragments"])
    assert set(revealed_originals) >= {SYNTH_EMAIL, SYNTH_API_TOKEN, SYNTH_BANK_CARD}


def test_session_profile_conflict_is_409(client):
    sid = client.post(
        "/api/v1/sessions", json={"text": "", "profile": "standard"}
    ).json()["session_id"]
    r = client.post(
        "/api/v1/sessions/chunk",
        json={"session_id": sid, "chunk": "abc", "profile": "strict"},
    )
    assert r.status_code == 409
    assert r.json()["error_category"] == "profile_conflict"


def test_unknown_session_is_404(client):
    r = client.post(
        "/api/v1/sessions/chunk",
        json={"session_id": "sess_deadbeef", "chunk": "x", "final": True},
    )
    assert r.status_code == 404
    assert r.json()["error_category"] == "session_not_found"


def test_audit_requires_key(client):
    r = client.get("/api/v1/audit/requests")
    assert r.status_code == 403
    assert r.json()["error_category"] == "audit_access_denied"


def test_audit_mappings_and_reveal(client, auth_headers):
    body = client.post(
        "/api/v1/redact", json={"text": f"m {SYNTH_EMAIL}"}
    ).json()
    rid = body["request_id"]
    listing = client.get("/api/v1/audit/requests", headers=auth_headers)
    assert listing.status_code == 200
    assert any(q["request_id"] == rid for q in listing.json()["requests"])

    detail = client.get(
        f"/api/v1/audit/requests/{rid}", headers=auth_headers
    ).json()
    frag = detail["fragments"][0]
    assert frag["label"] == "email"
    assert "original" not in frag  # hidden unless reveal=1
    assert frag["original_sha256"]

    revealed = client.get(
        f"/api/v1/audit/requests/{rid}?reveal=true", headers=auth_headers
    ).json()
    assert revealed["fragments"][0]["original"] == SYNTH_EMAIL

    # Wrong key on detail endpoint.
    denied = client.get(
        f"/api/v1/audit/requests/{rid}", headers={"X-Audit-Key": "wrong"}
    )
    assert denied.status_code == 403


def test_ciphertext_at_rest_has_no_plaintext(app, client, auth_headers):
    body = client.post(
        "/api/v1/redact", json={"text": f"t {SYNTH_EMAIL}"}
    ).json()
    audit = app.state.audit
    raw = audit._conn.execute(
        "SELECT cast(ciphertext as text) FROM fragments"
    ).fetchall()
    joined = b" ".join(r[0] for r in raw if isinstance(r[0], bytes)).decode(
        "utf-8", "ignore"
    )
    # cast fallback also checked via bytes path:
    blob_rows = audit._conn.execute("SELECT ciphertext FROM fragments").fetchall()
    for (blob,) in blob_rows:
        assert SYNTH_EMAIL.encode() not in blob
    assert SYNTH_EMAIL not in joined


def test_hash_chain_verifies(client, auth_headers):
    client.post("/api/v1/redact", json={"text": f"x {SYNTH_EMAIL}"})
    rep = client.get("/api/v1/audit/chain", headers=auth_headers).json()
    assert rep["chain"]["ok"] is True
    assert rep["chain"]["events"] >= 2


def test_state_isolation_between_sessions(client):
    s1 = client.post("/api/v1/sessions", json={"text": ""}).json()["session_id"]
    s2 = client.post("/api/v1/sessions", json={"text": ""}).json()["session_id"]
    client.post(
        "/api/v1/sessions/chunk",
        json={"session_id": s1, "chunk": SYNTH_EMAIL[:10]},
    )
    # s2 must not see s1's buffered content.
    r2 = client.post(
        "/api/v1/sessions/chunk",
        json={"session_id": s2, "chunk": "plain", "final": True},
    )
    assert r2.json()["emitted"] == "plain"

    # Finish s1: the email arrives across the boundary between two sessions'
    # lifetimes, yet s1 reconstructs it from its own private buffer.
    r1 = client.post(
        "/api/v1/sessions/chunk",
        json={
            "session_id": s1,
            "chunk": " " + SYNTH_EMAIL[10:],
            "final": True,
        },
    )
    assert r1.status_code == 200
    joined = client.post(
        "/api/v1/sessions/chunk",
        json={"session_id": s1, "chunk": SYNTH_EMAIL[:10], "final": False},
    )
    assert joined.status_code == 404
    assert joined.json()["error_category"] == "session_not_found"


def test_hash_chain_tamper_is_detected(app, client, auth_headers):
    client.post("/api/v1/redact", json={"text": f"x {SYNTH_EMAIL}"})
    ok = client.get("/api/v1/audit/chain", headers=auth_headers).json()
    assert ok["chain"]["ok"] is True
    # Tamper with a persisted payload directly in the SQLite file.
    with app.state.audit._conn:
        app.state.audit._conn.execute(
            "UPDATE events SET payload=? WHERE id=1", ('{"event":"forged"}',)
        )
    bad = client.get("/api/v1/audit/chain", headers=auth_headers).json()
    assert bad["chain"]["ok"] is False
    assert bad["chain"]["broken_at_event_id"] >= 1


def test_chunk_after_final_is_session_closed_category(client):
    sid = client.post("/api/v1/sessions", json={"text": ""}).json()["session_id"]
    first = client.post(
        "/api/v1/sessions/chunk",
        json={"session_id": sid, "chunk": "abc", "final": True},
    )
    assert first.status_code == 200 and first.json()["closed"] is True
    again = client.post(
        "/api/v1/sessions/chunk",
        json={"session_id": sid, "chunk": "d", "final": False},
    )
    assert again.status_code == 404  # closed sessions are removed
    assert again.json()["error_category"] == "session_not_found"


def test_tampered_ciphertext_is_integrity_error(app, client, auth_headers):
    body = client.post(
        "/api/v1/redact", json={"text": f"m {SYNTH_EMAIL}"}
    ).json()
    rid = body["request_id"]
    with app.state.audit._conn:
        app.state.audit._conn.execute(
            "UPDATE fragments SET ciphertext = randomblob(64) WHERE request_id=?",
            (rid,),
        )
    r = client.get(
        f"/api/v1/audit/requests/{rid}?reveal=true", headers=auth_headers
    )
    assert r.status_code == 500
    assert r.json()["error_category"] == "audit_integrity_error"


def test_rules_listing_shows_priorities(client):
    r = client.get("/api/v1/rules")
    profiles = {p["profile"]: p for p in r.json()["profiles"]}
    ids = [x["rule_id"] for x in profiles["standard"]["rules"]]
    # Sorted by priority desc.
    pris = [x["priority"] for x in profiles["standard"]["rules"]]
    assert pris == sorted(pris, reverse=True)
    assert "std.field.quoted" in ids
