"""HTTP API 测试：成功提交、四类错误的状态码与信封、失败状态保全。

使用 stdlib asyncio 驱动 httpx.AsyncClient(ASGITransport)，无需异步 pytest 插件。
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from utxo_ledger import encoding, fab
from utxo_ledger.api import create_app
from utxo_ledger.store import SqliteStore


class Api:
    """同步外观：内部维护一个事件循环与 ASGI 异步客户端。"""

    def __init__(self, ring, log_dir):
        self.loop = asyncio.new_event_loop()
        self.store = SqliteStore(":memory:")
        app = create_app(self.store, log_dir=log_dir)
        transport = httpx.ASGITransport(app=app)
        self.client = httpx.AsyncClient(
            transport=transport, base_url="http://t"
        )
        self.ring = ring

    def call(self, method, path, **kwargs):
        async def _go():
            return await self.client.request(method, path, **kwargs)

        return self.loop.run_until_complete(_go())

    def get(self, path):
        return self.call("GET", path)

    def post(self, path, **kwargs):
        return self.call("POST", path, **kwargs)

    def close(self):
        self.loop.run_until_complete(self.client.aclose())
        self.loop.close()


@pytest.fixture
def api(ring, tmp_path):
    a = Api(ring, str(tmp_path / "logs"))
    yield a
    a.close()


def _genesis_json(ring):
    g = fab.genesis_block([fab.issue_tx([(100, ring.pub(0))])])
    return g, encoding.block_to_json(g)


def test_health_and_genesis_submit(api):
    r = api.get("/health")
    assert r.json()["ok"] is True
    _g, gj = _genesis_json(api.ring)
    r = api.post("/blocks", json=gj)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["ok"] is True and body["height"] == 0
    assert len(body["run_id"]) >= 9
    tip = api.get("/chain/tip").json()
    assert tip["height"] == 0 and tip["utxo_count"] == 1


def test_malformed_json_is_input_error(api):
    r = api.post(
        "/blocks",
        content=b"{not json",
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["category"] == "INPUT_ERROR"


def test_unknown_key_rejected(api):
    _g, gj = _genesis_json(api.ring)
    gj["header"]["bogus"] = 3
    r = api.post("/blocks", json=gj)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "MALFORMED_ENCODING"


def test_double_spend_conflict_status_and_state_preserved(api):
    ring = api.ring
    g, gj = _genesis_json(ring)
    assert api.post("/blocks", json=gj).status_code == 201
    gid = encoding.txid_of(g.transactions[0])
    t1 = fab.sign_tx(
        fab.unsigned_tx(
            [encoding.Outpoint(gid, 0)],
            [fab.make_output(100, ring.pub(1))],
            fee=0,
        ),
        owner_privkeys=[ring.priv(0)],
    )
    t2 = fab.sign_tx(
        fab.unsigned_tx(
            [encoding.Outpoint(gid, 0)],
            [fab.make_output(100, ring.pub(2))],
            fee=0,
        ),
        owner_privkeys=[ring.priv(0)],
    )
    bad = encoding.block_to_json(fab.next_block([t1, t2], g))
    before = api.get("/chain/tip").json()
    r = api.post("/blocks", json=bad)
    assert r.status_code == 409
    body = r.json()
    assert body["error"]["code"] == "DOUBLE_SPEND"
    assert body["state_preserved"] is True
    after = api.get("/chain/tip").json()
    assert before["utxo_root"] == after["utxo_root"]
    assert after["height"] == 0


def test_zero_value_400_and_signature_422(api):
    ring = api.ring
    g, gj = _genesis_json(ring)
    api.post("/blocks", json=gj)
    gid = encoding.txid_of(g.transactions[0])

    zero = fab.sign_tx(
        fab.unsigned_tx(
            [encoding.Outpoint(gid, 0)],
            [fab.make_output(100, ring.pub(1)), fab.make_output(0, ring.pub(2))],
            fee=0,
        ),
        owner_privkeys=[ring.priv(0)],
    )
    rz = api.post("/blocks", json=encoding.block_to_json(fab.next_block([zero], g)))
    assert rz.status_code == 400
    assert rz.json()["error"]["code"] == "ZERO_VALUE"

    tamper = fab.sign_tx(
        fab.unsigned_tx(
            [encoding.Outpoint(gid, 0)],
            [fab.make_output(100, ring.pub(1))],
            fee=0,
        ),
        owner_privkeys=[ring.priv(0)],
    )
    sig = bytearray(tamper.witnesses[0].signature)
    sig[-1] ^= 1
    tamper = fab.with_witnesses(tamper, [bytes(sig)])
    rs = api.post("/blocks", json=encoding.block_to_json(fab.next_block([tamper], g)))
    assert rs.status_code == 422
    assert rs.json()["error"]["code"] == "SIGNATURE_INVALID"


def test_utxo_endpoint_states(api):
    ring = api.ring
    g, gj = _genesis_json(ring)
    api.post("/blocks", json=gj)
    gid = encoding.txid_of(g.transactions[0])
    r = api.get(f"/utxo/{gid.hex()}/0")
    assert r.status_code == 200 and r.json()["utxo"]["amount"] == 100
    r = api.get(f"/utxo/{'ff' * 32}/0")
    assert r.status_code == 404 and r.json()["exists"] is False
    r = api.get(f"/address/{ring.pub(0).hex()}/utxos")
    assert r.json()["count"] == 1
