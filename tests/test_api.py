"""HTTP boundary tests: status mapping, transport errors, end-to-end submit."""

from __future__ import annotations

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from utxo_ledger.api import create_app  # noqa: E402
from utxo_ledger.encoding import Outpoint, encode_block  # noqa: E402

from tests.fixtures import (  # noqa: E402
    FixtureBuilder,
    coinbase_tx,
    named_key,
    transfer_tx,
)


@pytest.fixture()
def client():
    app = create_app(db_path=":memory:")
    with TestClient(app) as c:
        yield c


def _funded_raw():
    alice = named_key("alice")
    fb = FixtureBuilder()
    b1 = fb.append([coinbase_tx(1, [(1_000_000, alice.public_bytes)])])
    return fb, alice, b1


def test_health_and_empty_tip(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["tip"]["height"] == 0


def test_submit_valid_block_returns_201_and_tip(client):
    fb, alice, _ = _funded_raw()
    r = client.post("/blocks", json={"raw_hex": fb.raw_blocks[0].hex()})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["accepted"] is True
    assert body["height"] == 1
    assert body["state_unchanged"] is False
    assert client.get("/chain/tip").json()["height"] == 1


def test_double_spend_is_409_state_conflict_and_unchanged(client):
    fb, alice, b1 = _funded_raw()
    assert client.post("/blocks", json={"raw_hex": fb.raw_blocks[0].hex()}).status_code == 201
    bob, carol = named_key("bob"), named_key("carol")
    src = Outpoint(b1.transactions[0].txid, 0)
    tx1 = transfer_tx(
        [(src, alice.public_bytes)],
        [(400_000, bob.public_bytes), (599_000, alice.public_bytes)],
        {0: alice},
    )
    tx2 = transfer_tx(
        [(src, alice.public_bytes)],
        [(400_000, carol.public_bytes), (599_000, alice.public_bytes)],
        {0: alice},
    )
    block = fb.append([coinbase_tx(2, [(1_000_000, alice.public_bytes)]), tx1, tx2])
    r = client.post("/blocks", json={"raw_hex": encode_block(block).hex()})
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "double_spend"
    assert err["category"] == "state"
    assert err["tx_index"] == 2
    # Tip stays at 1 and state_unchanged flag is reported.
    assert r.json()["state_unchanged"] is True
    assert client.get("/chain/tip").json()["height"] == 1


def test_malformed_hex_is_400_input(client):
    r = client.post("/blocks", json={"raw_hex": "zz"})
    assert r.status_code == 400
    assert r.json()["error"]["category"] == "input"


def test_oversized_count_is_422_resource(client):
    raw = (
        b"UTXO-BLOCK/1\x00"
        + (1).to_bytes(4, "little")
        + (1).to_bytes(8, "little")
        + b"\x00" * 32
        + (10_000).to_bytes(4, "little")
    )
    r = client.post("/blocks", json={"raw_hex": raw.hex()})
    assert r.status_code == 422
    assert r.json()["error"]["category"] == "resource"
    assert r.json()["error"]["code"] == "too_many_txs"


def test_read_paths(client):
    fb, _, _ = _funded_raw()
    client.post("/blocks", json={"raw_hex": fb.raw_blocks[0].hex()})
    assert client.get("/chain/blocks/1").status_code == 200
    assert client.get("/chain/blocks/999").status_code == 400
    assert client.get("/chain/txs/ab").status_code == 400  # bad hex length
    assert client.get("/utxos").status_code == 200
