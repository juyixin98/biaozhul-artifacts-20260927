"""HTTP API tests through an in-process ASGI client."""
from __future__ import annotations

import pytest

httpx = pytest.importorskip("httpx")

from abibackend import api as api_module  # noqa: E402
from abibackend.storage import Repository  # noqa: E402


@pytest.fixture
def client(tmp_path):
    repo = Repository(str(tmp_path / "api.sqlite3"))
    api_module._repo = repo
    # httpx TestClient for ASGI without opening a socket
    with httpx.Client(transport=httpx.ASGITransport(app=api_module.app), base_url="http://test") as c:
        c.headers["x-run-id"] = "api-test"
        yield c
    repo.close()
    api_module._repo = None


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.headers.get("x-run-id")


def test_encode_decode_roundtrip(client):
    r = client.post("/abi/encode", json={
        "types": ["uint256", "bytes", "int256[]", "(string,uint256)"],
        "values": [42, "0xcafe", [-1, -2], ["nested", 9]],
    })
    assert r.status_code == 200, r.text
    data = r.json()["data"]

    r2 = client.post("/abi/decode", json={"types": ["uint256", "bytes", "int256[]", "(string,uint256)"], "data": data})
    assert r2.status_code == 200, r2.text
    assert r2.json()["values"] == [42, "0xcafe", [-1, -2], ["nested", 9]]


def test_selector_known(client):
    r = client.post("/abi/selector", json={"name": "transfer", "types": ["address", "uint256"]})
    assert r.status_code == 200
    assert r.json()["selector"] == "0xa9059cbb"


def test_decode_error_has_specific_category(client):
    # bytes with offset into head -> offset_out_of_bounds, HTTP 400
    evil = "0x" + ("00" * 32) + ("40" + "00" * 31)  # ptr0=0, ptr1=0x40... craft minimal
    # Use a single bytes with offset pointing into head region:
    payload = (0).to_bytes(32, "big").hex()  # offset 0 for a dynamic bytes
    r = client.post("/abi/decode", json={"types": ["bytes"], "data": "0x" + payload})
    assert r.status_code == 400
    body = r.json()
    assert body["error"]["code"] == "offset_out_of_bounds"


def test_replay_and_query(client):
    r = client.post("/replay")
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["total"] == 9
    assert rep["failed"] >= 1

    r2 = client.get("/runs")
    assert r2.status_code == 200
    assert any(x["run_id"] == rep["run_id"] for x in r2.json()["runs"])

    r3 = client.get("/transactions")
    assert len(r3.json()["transactions"]) == 9
