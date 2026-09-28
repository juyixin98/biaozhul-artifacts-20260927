"""End-to-end HTTP integration tests with a real FastAPI app + SQLite DB."""
from __future__ import annotations


import pytest
from fastapi.testclient import TestClient

from smt.api.app import create_app
from smt.config import Settings

KA = "00" * 30 + "ab" + "00"
KB = "00" * 30 + "ab" + "01"
KC = "ff" * 32
ABS_SUB = "00" * 30 + "ab" + "02"


@pytest.fixture()
def client(tmp_path):
    settings = Settings(
        sqlite_path=str(tmp_path / "api.db"),
        journal_hmac_key="integration-key",
        log_format="text",
    )
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def test_health_and_empty_root(client):
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["spec"] == "smt-v1"
    assert body["root"]
    empty_root = body["root"]

    r2 = client.get("/api/v1/root")
    assert r2.json()["root"] == empty_root
    # request id is echoed and stable per request
    assert r2.headers["x-request-id"]


def test_batch_update_then_get_and_prove(client):
    r = client.post("/api/v1/updates", json={"updates": [
        {"key": KC, "value": "gamma"},
        {"key": KA, "value": "alpha"},
        {"key": KB, "value": "beta"},
    ]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["effects"]) == 3
    assert body["revision"] >= 2
    root_after_batch = body["root"]

    # deterministic ordering: effects come back sorted by key
    assert [e["key"] for e in body["effects"]] == sorted([KA, KB, KC])

    # GET a value
    g = client.get(f"/api/v1/values/{KA}")
    assert g.status_code == 200
    assert g.json()["exists"] is True and g.json()["value"] == "alpha"
    assert g.json()["root"] == root_after_batch

    # GET absent key
    ga = client.get(f"/api/v1/values/{ABS_SUB}")
    assert ga.json()["exists"] is False and ga.json()["value"] is None

    # membership proof verifies through the verify endpoint
    p = client.get(f"/api/v1/proofs/{KA}").json()["proof"]
    assert p["exists"] is True and p["terminal_depth"] == 256
    v = client.post("/api/v1/proofs/verify", json={"proof": p})
    assert v.json()["valid"] is True and v.json()["verdict"] == "valid"

    # non-membership proof inside the shared-prefix subtree
    pn = client.get(f"/api/v1/proofs/{ABS_SUB}").json()["proof"]
    assert pn["exists"] is False
    assert client.post("/api/v1/proofs/verify", json={"proof": pn}).json()["valid"]


def test_batch_equivalence_end_to_end(client, tmp_path):
    """Batch root equals one-by-one (sorted) root on a SEPARATE service."""
    client.post("/api/v1/updates", json={"updates": [
        {"key": KC, "value": "gamma"},
        {"key": KA, "value": "alpha"},
        {"key": KB, "value": "beta"},
    ]})
    batch_root = client.get("/api/v1/root").json()["root"]

    settings2 = Settings(
        sqlite_path=str(tmp_path / "api2.db"), journal_hmac_key="integration-key"
    )
    with TestClient(create_app(settings2)) as c2:
        for k in [KA, KB, KC]:
            val = {KA: "alpha", KB: "beta", KC: "gamma"}[k]
            assert c2.post("/api/v1/updates", json={"updates": [{"key": k, "value": val}]}).status_code == 200
        assert c2.get("/api/v1/root").json()["root"] == batch_root


def test_delete_then_restore_returns_same_root(client):
    client.post("/api/v1/updates", json={"updates": [
        {"key": KA, "value": "alpha"}, {"key": KB, "value": "beta"},
    ]})
    r_full = client.get("/api/v1/root").json()["root"]

    client.post("/api/v1/updates", json={"updates": [{"key": KA, "value": None}]})
    r_del = client.get("/api/v1/root").json()["root"]
    assert r_del != r_full
    # kb survives the delete
    assert client.get(f"/api/v1/values/{KB}").json()["exists"] is True
    assert client.get(f"/api/v1/values/{KA}").json()["exists"] is False

    client.post("/api/v1/updates", json={"updates": [{"key": KA, "value": "alpha"}]})
    assert client.get("/api/v1/root").json()["root"] == r_full


def test_historical_root_still_serves_and_verifies(client):
    client.post("/api/v1/updates", json={"updates": [{"key": KA, "value": "alpha"}]})
    r1 = client.get("/api/v1/root").json()["root"]
    client.post("/api/v1/updates", json={"updates": [{"key": KB, "value": "beta"}]})

    # old root is listed in revision history
    revs = client.get("/api/v1/revisions").json()["revisions"]
    roots = {rv["root"] for rv in revs}
    assert r1 in roots

    # historical proof against r1: ka exists, kb does not
    pka = client.get(f"/api/v1/proofs/{KA}", params={"root": r1}).json()["proof"]
    assert pka["root"] == r1 and pka["exists"] is True
    pkb = client.get(f"/api/v1/proofs/{KB}", params={"root": r1}).json()["proof"]
    assert pkb["exists"] is False
    assert client.post("/api/v1/proofs/verify", json={"proof": pkb}).json()["valid"]

    # historical GET
    old_get = client.get(f"/api/v1/values/{KB}", params={"root": r1})
    assert old_get.json()["exists"] is False


def test_duplicate_key_in_batch_rejected_409(client):
    r = client.post("/api/v1/updates", json={"updates": [
        {"key": KA, "value": "a"}, {"key": KA, "value": "b"},
    ]})
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["category"] == "duplicate_key"
    assert r.json()["request_id"]
    # root unchanged (still empty)
    assert client.get(f"/api/v1/values/{KA}").json()["exists"] is False


def test_malformed_key_rejected_400(client):
    # wrong length and non-hex keys fail normalization in the service layer
    r = client.post("/api/v1/updates", json={"updates": [{"key": "zz", "value": "a"}]})
    assert r.status_code == 400
    assert r.json()["error"]["category"] == "malformed_input"

    r2 = client.post("/api/v1/updates", json={"updates": [{"key": "zz" * 32, "value": "a"}]})
    assert r2.status_code == 400
    assert r2.json()["error"]["category"] == "malformed_input"

    g = client.get("/api/v1/values/not-hex")
    assert g.status_code == 400 and g.json()["error"]["category"] == "malformed_input"


def test_verify_endpoint_reports_failure_categories(client):
    client.post("/api/v1/updates", json={"updates": [{"key": KA, "value": "alpha"}]})
    p = client.get(f"/api/v1/proofs/{KA}").json()["proof"]

    tampered = dict(p)
    tampered["root"] = "ab" * 32
    r = client.post("/api/v1/proofs/verify", json={"proof": tampered}).json()
    assert r["valid"] is False and r["verdict"] == "root_mismatch"

    bad_version = {**p, "version": "nope"}
    r2 = client.post("/api/v1/proofs/verify", json={"proof": bad_version}).json()
    assert r2["verdict"] == "malformed"


def test_empty_batch_rejected_by_schema(client):
    r = client.post("/api/v1/updates", json={"updates": []})
    assert r.status_code == 422


def test_request_id_is_honored_and_echoed(client):
    rid = "fixed-correlation-id-1234"
    r = client.get("/api/v1/root", headers={"X-Request-ID": rid})
    assert r.headers["x-request-id"] == rid
    assert r.json()["request_id"] == rid


def test_historical_root_malformed_hex_is_400(client):
    r = client.get(f"/api/v1/proofs/{KA}", params={"root": "nothex"})
    assert r.status_code == 400
    assert r.json()["error"]["category"] == "malformed_input"


def test_historical_unknown_root_is_undecidable_404(client):
    # a syntactically valid root the store has no nodes for is not "absent";
    # the service reports it cannot decide.
    phantom = "ab" * 32
    r = client.get(f"/api/v1/proofs/{KA}", params={"root": phantom})
    assert r.status_code == 404
    assert r.json()["error"]["category"] == "unknown_root"
    g = client.get(f"/api/v1/values/{KA}", params={"root": phantom})
    assert g.status_code == 404 and g.json()["error"]["category"] == "unknown_root"


def test_compressed_and_raw_proofs_both_verify(client):
    client.post("/api/v1/updates", json={"updates": [
        {"key": KA, "value": "alpha"}, {"key": KB, "value": "beta"},
    ]})
    comp = client.get(f"/api/v1/proofs/{KA}", params={"compress": "true"}).json()["proof"]
    raw = client.get(f"/api/v1/proofs/{KA}", params={"compress": "false"}).json()["proof"]
    assert len(comp["steps"]) < len(raw["steps"])
    assert len(raw["steps"]) == 256
    for p in (comp, raw):
        assert client.post("/api/v1/proofs/verify", json={"proof": p}).json()["valid"]
