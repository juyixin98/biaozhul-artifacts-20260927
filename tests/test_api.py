"""End-to-end HTTP tests using FastAPI's TestClient (httpx in-process)."""
from __future__ import annotations

import pytest

from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Settings


SECRET_HEX = b"api-level secret".hex()


@pytest.fixture()
def client(tmp_path):
    settings = Settings(db_path=str(tmp_path / "api.db"), audit_to_stderr=False)
    app = create_app(settings)
    with TestClient(app) as c:
        yield c
    app.state.store.close()


def _create(client, t=3, n=5, secret_hex=SECRET_HEX, cid=None):
    body = {"secret_hex": secret_hex, "threshold": t, "total": n}
    if cid:
        body["collection_id"] = cid
    r = client.post("/collections", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_create_and_recover_roundtrip(client):
    created = _create(client)
    cid = created["collection_id"]
    rid = created["request_id"]
    assert len(created["shares"]) == 5

    subset = [created["shares"][i] for i in (0, 2, 4)]
    r = client.post(
        f"/collections/{cid}/recover",
        json={"shares": subset},
        headers={"X-Request-ID": "req-e2e-1"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "recovered_unverifiable"
    assert data["accepted"] is True
    assert data["secret_hex"] == SECRET_HEX
    assert r.headers["X-Request-ID"] == "req-e2e-1"
    # the create response carried a server request id too
    assert rid.startswith("req_")


def test_verified_recovery_with_extra_shares(client):
    created = _create(client, t=2, n=4)
    cid = created["collection_id"]
    r = client.post(
        f"/collections/{cid}/recover", json={"shares": created["shares"]}
    )
    data = r.json()
    assert data["status"] == "recovered_verified"
    assert sorted(data["extra_xs"]) == [3, 4]
    assert data["secret_hex"] == SECRET_HEX


def test_below_threshold_is_structured_failure(client):
    created = _create(client)
    cid = created["collection_id"]
    r = client.post(
        f"/collections/{cid}/recover",
        json={"shares": created["shares"][:2]},
    )
    data = r.json()
    assert r.status_code == 200
    assert data["accepted"] is False
    assert data["status"] == "rejected_insufficient_threshold"
    assert "secret_hex" not in data


def test_tampered_share_rejected_over_http(client):
    created = _create(client)
    cid = created["collection_id"]
    bad = dict(created["shares"][0])
    bad["ys"][0] = str(int(bad["ys"][0]) + 1)
    r = client.post(
        f"/collections/{cid}/recover",
        json={"shares": [bad, created["shares"][1], created["shares"][2]]},
    )
    data = r.json()
    assert data["status"] == "rejected_insufficient_threshold"
    reasons = {x["reason"] for x in data["rejected_shares"]}
    assert "bad_integrity_mac" in reasons


def test_foreign_collection_404_and_meta_endpoint(client):
    created = _create(client, cid="coll_meta")
    r = client.post(
        "/collections/does_not_exist/recover", json={"shares": created["shares"]}
    )
    assert r.status_code == 404

    meta = client.get("/collections/coll_meta").json()
    assert meta["share_count"] == 5
    # metadata exposes fingerprints, never ys/mac
    for view in meta["shares"]:
        assert "ys" not in view and "mac" not in view
        assert view["fingerprint"].startswith("sha256:")


def test_audit_endpoint_fingerprint_only(client):
    created = _create(client, cid="coll_aud")
    cid = created["collection_id"]
    client.post(
        f"/collections/{cid}/recover",
        json={"shares": created["shares"][:3]},
        headers={"X-Request-ID": "req-audit-e2e"},
    )
    r = client.get("/audit", params={"request_id": "req-audit-e2e"})
    events = r.json()["events"]
    assert events and events[0]["verdict"] == "recovered_unverifiable"
    for e in events:
        assert SECRET_HEX not in str(e)


def test_invalid_create_params_400(client):
    r = client.post(
        "/collections", json={"secret_hex": "ab", "threshold": 5, "total": 3}
    )
    assert r.status_code == 400


def test_bad_hex_422(client):
    r = client.post(
        "/collections", json={"secret_hex": "zz", "threshold": 2, "total": 3}
    )
    assert r.status_code == 422
