"""HTTP API tests via FastAPI's in-process test client (real uvicorn-free ASGI)."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from reorgindex.api.main import create_app
from reorgindex.config import Settings

pytestmark = pytest.mark.api


@pytest.fixture
def client(tmp_path, short_fork_docs):
    recording, _ = short_fork_docs
    settings = Settings(
        database_path=tmp_path / "api.db",
        finality_depth=3,
        allowed_difficulties=frozenset({4, 16}),
        service_name="test",
        log_level="ERROR",
    )
    # Patch producer config lookup by monkeypatching create_app's read.
    app = create_app(settings)
    # create_app reads config from repo config/; fixtures use that producer.
    with TestClient(app) as c:
        c._recording = recording  # type: ignore[attr-defined]
        yield c


def _post(client: TestClient, name: str, *, request_id: str | None = None, crash: bool = False):
    by_name = {b["name"]: b["block"] for b in client._recording["blocks"]}  # type: ignore[attr-defined]
    body = dict(by_name[name])
    if crash:
        body["__crash_point__"] = "after_detach"
    headers = {"X-Request-ID": request_id} if request_id else {}
    return client.post("/blocks", json=body, headers=headers)


def test_health_and_empty_chain(client):
    assert client.get("/health").json() == {"status": "ok"}
    chain = client.get("/chain").json()
    assert chain["tip"] is None and chain["height"] is None
    assert chain["finality_depth"] == 3


def test_submit_genesis_and_query_account(client):
    r = _post(client, "g0", request_id="rid-g")
    assert r.status_code == 200
    assert r.headers["x-request-id"] == "rid-g"
    body = r.json()
    assert body["result"]["outcome"] == "ACCEPT_EXTEND"
    addrs = client._recording["addresses"]  # type: ignore[attr-defined]
    acct = client.get(f"/accounts/{addrs['alice']}").json()
    assert acct["balance"] == 1000 and acct["nonce"] == 0


def test_orphan_returns_pending_and_listed(client):
    _post(client, "g0")
    r = _post(client, "o1")
    assert r.status_code == 200
    assert r.json()["result"]["outcome"] == "PENDING"
    pending = client.get("/pending").json()["pending"]
    assert len(pending) == 1 and pending[0]["height"] == 3


def test_bad_block_returns_422_with_stable_code(client):
    block = dict(next(b["block"] for b in client._recording["blocks"] if b["name"] == "m1"))  # type: ignore[attr-defined]
    block["difficulty"] = 99
    r = client.post("/blocks", json=block, headers={"X-Request-ID": "rid-bad"})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "BAD_DIFFICULTY"
    assert err["request_id"] == "rid-bad"
    assert "state" in err
    # The stateless rejection is also auditable with its request id.
    diag = client.get("/diagnostics?limit=100").json()["diagnostics"]
    assert any(d["request_id"] == "rid-bad" and d["reason"] == "BAD_DIFFICULTY" for d in diag)


def test_full_scenario_switch_and_duplicate_query(client):
    for name in ("g0", "m1", "m2", "m3", "o1", "f1", "f2"):
        r = _post(client, name)
        assert r.status_code == 200, name

    chain = client.get("/chain").json()
    recording = client._recording  # type: ignore[attr-defined]
    from tests.oracle.reference import load_blocks
    blocks = load_blocks(recording)
    assert chain["tip"] == blocks["o1"].hash
    assert chain["height"] == 3

    dup = recording["expected"]["duplicate_txid"]
    tx = client.get(f"/transactions/{dup}").json()
    assert len(tx["occurrences"]) == 2
    assert tx["active_occurrence"] == blocks["f1"].hash
    assert tx["contributes_to_best_chain"] is True

    # Block detail with confirmations/finality.
    detail = client.get(f"/chain/block/{blocks['g0'].hash}").json()
    assert detail["on_active"] is True
    assert detail["confirmations"] == 4 and detail["final"] is True

    # Diagnostics endpoint carries request ids and state; no private keys.
    diag = client.get("/diagnostics?limit=100").json()["diagnostics"]
    outcomes = {d["outcome"] for d in diag}
    assert "ACCEPT_SWITCH" in outcomes and "PENDING" in outcomes
    raw = json.dumps(diag)
    assert "BEGIN PRIVATE KEY" not in raw


def test_deep_fork_422_finalized_and_resume_endpoint(tmp_path, deep_fork_docs):
    recording, _ = deep_fork_docs
    settings = Settings(
        database_path=tmp_path / "api-deep.db", finality_depth=3,
        allowed_difficulties=frozenset({4, 16}), service_name="test", log_level="ERROR",
    )
    with TestClient(create_app(settings)) as client:
        by_name = {b["name"]: b["block"] for b in recording["blocks"]}
        for name in ("g0", "m1", "m2", "m3", "m4", "m5", "d1"):
            assert client.post("/blocks", json=by_name[name]).status_code == 200
        r = client.post("/blocks", json=by_name["d2"])
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "REORG_FINALIZED"
        # resume endpoint remains healthy
        resumed = client.post("/chain/resume").json()
        assert resumed["released_pending"] == []
