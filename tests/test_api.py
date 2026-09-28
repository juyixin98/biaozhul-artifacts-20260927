"""HTTP API tests via in-process ASGI transport (no real network socket)."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

httpx = pytest.importorskip("httpx")
import pytest_asyncio  # noqa: E402

from basefee.api import create_app  # noqa: E402
from basefee.params import PARAMS  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from reference import oracle as O  # noqa: E402


@pytest_asyncio.fixture
async def client(tmp_path):
    db = str(tmp_path / "api.db")
    app = create_app(db_path=db)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://test") as c:
        yield c, app


async def _post(c, path, **kw):
    return await c.post(path, **kw)


async def test_health_reports_version_and_boundaries(client):
    c, _ = client
    r = await c.get("/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["protocol_version"] == PARAMS.protocol_version
    assert "no real chain connection" in body["boundaries"][0]
    assert r.headers["X-Protocol-Version"] == PARAMS.protocol_version
    assert "X-Request-ID" in r.headers


async def test_fee_next_hand_vectors(client, hand_vectors):
    c, _ = client
    for v in hand_vectors["base_fee_vectors"]:
        r = await c.post("/v1/fee/next", json={
            "parent_base_fee": str(v["parent_base_fee"]),
            "gas_used": v["gas_used"], "gas_limit": v["gas_limit"]})
        assert r.status_code == 200, v["name"]
        assert r.json()["result"]["next_base_fee"] == str(v["expected_next_base_fee"])


async def test_fee_next_gas_exceeded_is_422_E040(client):
    c, _ = client
    r = await c.post("/v1/fee/next", json={"parent_base_fee": "1000",
                                           "gas_used": 99, "gas_limit": 10})
    assert r.status_code == 422
    assert r.json()["failures"][0]["code"] == "E040_BLOCK_GAS_EXCEEDED"


async def test_effective_tip_warning_separated(client):
    c, _ = client
    r = await c.post("/v1/fee/effective-tip", json={
        "base_fee": "1000000000", "max_fee_per_gas": "1200000000",
        "max_priority_fee_per_gas": "5000000000", "gas_limit": 21000})
    assert r.status_code == 200
    body = r.json()
    assert body["result"]["effective_priority_tip"] == "200000000"
    assert body["warnings"][0]["code"] == "W001_PRIORITY_CAP_ABOVE_FEE_CAP"
    assert body["failures"] == []


async def test_submit_full_fixture_and_query(client, chain_fixture):
    c, app = client
    for addr, bal in chain_fixture["genesis_balances"].items():
        app.state.chain.state.balances[addr] = int(bal)
    last = app.state.chain.genesis_hash()
    for block in chain_fixture["blocks"]:
        block = copy.deepcopy(block)
        if isinstance(block["parent_hash"], str) and block["parent_hash"].startswith("<"):
            block["parent_hash"] = last
        r = await c.post("/v1/blocks", json={
            "number": block["number"], "parent_hash": block["parent_hash"],
            "base_fee_per_gas": block["base_fee_per_gas"],
            "gas_limit": block["gas_limit"], "gas_used": block["gas_used"],
            "transactions": block["transactions"]})
        assert r.status_code == 201, (block["number"], r.text)
        body = r.json()
        assert body["result"]["next_base_fee"] == block["expected"]["next_base_fee"]
        assert isinstance(body["failures"], list)
        assert body["request_id"]
        last = body["result"]["block_hash"]

    r = await c.get("/v1/blocks/1")
    body = r.json()["result"]
    valid = [t for t in body["transactions"] if t["valid"]]
    assert len(valid) == 1
    assert valid[0]["burned"] == chain_fixture["blocks"][0]["expected"]["burned"]

    r = await c.get("/v1/blocks/head")
    assert r.json()["result"]["number"] == 4

    r = await c.get("/v1/blocks/99")
    assert r.status_code == 404
    assert r.json()["failures"][0]["code"] == "E404_NOT_FOUND"


async def test_submit_block_gas_exceeded_422(client):
    c, _ = client
    r = await c.post("/v1/blocks", json={
        "number": 1, "parent_hash": "0x" + "00" * 32,
        "base_fee_per_gas": str(PARAMS.genesis_base_fee),
        "gas_limit": 30_000_000, "gas_used": 30_000_001, "transactions": []})
    assert r.status_code == 422
    assert r.json()["failures"][0]["code"] == "E040_BLOCK_GAS_EXCEEDED"


async def test_bad_wire_hex_is_E001(client, chain_fixture):
    c, app = client
    tx = copy.deepcopy(chain_fixture["blocks"][0]["transactions"][0])
    tx["to"] = "0xZZ"
    r = await c.post("/v1/blocks", json={
        "number": 1, "parent_hash": app.state.chain.genesis_hash(),
        "base_fee_per_gas": str(PARAMS.genesis_base_fee),
        "gas_limit": 30_000_000, "gas_used": 21000, "transactions": [tx]})
    assert r.status_code == 422
    assert r.json()["failures"][0]["code"] == "E001_HEX_DECODE"


async def test_duplicate_submit_returns_409(client):
    c, app = client
    payload = {
        "number": 1, "parent_hash": app.state.chain.genesis_hash(),
        "base_fee_per_gas": str(PARAMS.genesis_base_fee),
        "gas_limit": 30_000_000, "gas_used": 0, "transactions": []}
    assert (await c.post("/v1/blocks", json=payload)).status_code == 201
    # reset in-memory chain to genesis; durable store still holds block 1
    from basefee.kernel import Chain
    from basefee.kernel.execution import ChainState
    app.state.chain = Chain(ChainState())
    r = await c.post("/v1/blocks", json=payload)
    assert r.status_code == 409
    assert r.json()["failures"][0]["code"] == "E050_BLOCK_EXISTS"


async def test_raw_rlp_transaction_accepted(client):
    sk = O.oracle_key(1)
    to = bytes(20)
    fields = [O._scalar(O.CHAIN_ID), O._scalar(0), O._scalar(2_000_000_000),
              O._scalar(100_000_000), O._scalar(21000), to, O._scalar(0), b""]
    sig = O.oracle_sign(sk, nonce=0, max_fee=2_000_000_000, max_tip=100_000_000,
                        gas_limit=21000, to=to, value=0)
    signed = fields + [
        O._scalar(sig["r"]), O._scalar(sig["s"]), O._scalar(sig["v"])]
    raw = "0x" + O.rlp_encode(signed).hex()

    c, app = client
    addr = O.oracle_address(O.oracle_pub_raw(sk))
    app.state.chain.state.balances[addr] = 10**24
    r = await c.post("/v1/blocks", json={
        "number": 1, "parent_hash": app.state.chain.genesis_hash(),
        "base_fee_per_gas": str(PARAMS.genesis_base_fee),
        "gas_limit": 30_000_000, "gas_used": 21000,
        "raw_transactions": [raw]})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["result"]["receipts"][0]["valid"] is True
    assert body["result"]["receipts"][0]["sender"] == addr
