"""测试共享：确定性密钥、区块构造器、空内核工厂。

这些构造器只使用 crypto 原语，不预置任何链结果；期望值要么手工指定，
要么来自独立的 reference 预言机 / 夹具目录，不调用被测内核生成答案。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from reorgindex import crypto
from reorgindex.config import Settings
from reorgindex.diagnostics import JsonDiagnostics
from reorgindex.kernel import ChainKernel
from reorgindex.storage import Storage

FINALITY_DEPTH = 6
FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures"


def _key(byte: int):
    priv = Ed25519PrivateKey.from_private_bytes(bytes([byte]) * 32)
    return priv, priv.public_key().public_bytes_raw().hex()


@pytest.fixture(scope="session")
def keys():
    prop_priv, prop_pub = _key(0x11)
    alice_priv, alice_pub = _key(0x22)
    bob_priv, bob_pub = _key(0x33)
    return {
        "proposer": (prop_priv, prop_pub),
        "alice": (alice_priv, alice_pub, crypto.address_of_pubkey(alice_pub)),
        "bob": (bob_priv, bob_pub, crypto.address_of_pubkey(bob_pub)),
        "carol": "c0" + "ab" * 31,
    }


def make_tx(priv, sender_pub, recipient, amount, nonce, memo=""):
    body = {"sender_pubkey": sender_pub, "recipient": recipient,
            "amount": amount, "nonce": nonce, "memo": memo}
    body["tx_id"] = crypto.tx_id_of(body)
    body["signature"] = crypto.sign_tx(priv, body)
    return body


def make_block(prop_priv, prop_pub, prev_hash, height, txs, weight):
    header = {
        "version": crypto.BLOCK_VERSION,
        "prev_hash": prev_hash,
        "height": height,
        "merkle_root": crypto.merkle_root([t["tx_id"] for t in txs]),
        "weight": weight,
        "proposer": prop_pub if height > 0 else "",
        "signature": "",
    }
    header["block_hash"] = crypto.block_hash_of(header)
    if height > 0:
        header["signature"] = crypto.sign_block(prop_priv, header)
    return {"header": header, "txs": txs}


@pytest.fixture()
def make(keys):
    prop_priv, prop_pub = keys["proposer"]

    def _make_block(prev_hash, height, txs=None, weight=1):
        return make_block(prop_priv, prop_pub, prev_hash, height, txs or [], weight)

    return _make_block


@pytest.fixture()
def env():
    """返回构造好的 (storage, kernel, diagnostics)，每个测试隔离内存库。"""

    storage = Storage(":memory:")
    settings = Settings(db_path=":memory:", finality_depth=FINALITY_DEPTH)
    diagnostics = JsonDiagnostics(level="WARNING")
    kernel = ChainKernel(storage, settings, diagnostics)
    yield storage, kernel, diagnostics
    storage.close()


@pytest.fixture()
def genesis(make):
    return make(crypto.ZERO_HASH, 0, [], 1)


def load_fixture(name: str) -> list[dict]:
    blocks = []
    with open(FIXTURE_DIR / f"{name}.jsonl", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                blocks.append(json.loads(line))
    return blocks


def load_expected(name: str) -> dict:
    return json.loads((FIXTURE_DIR / f"{name}.expected.json").read_text(encoding="utf-8"))
