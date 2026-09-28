"""编码/验签单元测试：固定向量、确定性、篡改必须按类别拒绝。"""

import pytest

from reorgindex import crypto
from reorgindex.errors import DecodeError, VerificationError

from .conftest import make_tx


def test_canonical_json_is_deterministic():
    obj = {"b": 2, "a": [1, {"c": 3}], "d": "中文"}
    assert crypto.canonical_json(obj) == crypto.canonical_json(
        {"d": "中文", "a": [1, {"c": 3}], "b": 2})
    assert crypto.canonical_json({"b": 2, "a": 1}) == '{"a":1,"b":2}'


def test_tx_id_is_stable_and_hex():
    body = {"sender_pubkey": "11" * 32, "recipient": "22" * 32,
            "amount": 5, "nonce": 0, "memo": ""}
    txid = crypto.tx_id_of(body)
    assert len(txid) == 64
    assert txid == crypto.tx_id_of(dict(body))
    # 改任意字段，交易号必变
    changed = dict(body, amount=6)
    assert crypto.tx_id_of(changed) != txid


def test_sign_and_verify_tx_roundtrip(keys):
    alice_priv, alice_pub, alice = keys["alice"]
    tx = {"sender_pubkey": alice_pub, "recipient": keys["carol"],
          "amount": 100, "nonce": 0, "memo": "pay"}
    tx["tx_id"] = crypto.tx_id_of(tx)
    tx["signature"] = crypto.sign_tx(alice_priv, tx)
    crypto.verify_tx(tx)  # 不抛即通过


def test_tampered_amount_rejected(keys):
    alice_priv, alice_pub, _ = keys["alice"]
    tx = {"sender_pubkey": alice_pub, "recipient": keys["carol"],
          "amount": 100, "nonce": 0, "memo": ""}
    tx["tx_id"] = crypto.tx_id_of(tx)
    tx["signature"] = crypto.sign_tx(alice_priv, tx)
    tx["amount"] = 101  # 篡改金额，签名/交易号均失效
    with pytest.raises(VerificationError):
        crypto.verify_tx(tx)


def test_forged_signature_rejected(keys):
    alice_priv, alice_pub, _ = keys["alice"]
    bob_priv, bob_pub, _ = keys["bob"]
    tx = {"sender_pubkey": alice_pub, "recipient": keys["carol"],
          "amount": 1, "nonce": 0, "memo": ""}
    tx["tx_id"] = crypto.tx_id_of(tx)
    tx["signature"] = crypto.sign_tx(bob_priv, tx)  # 用别人私钥签
    with pytest.raises(VerificationError):
        crypto.verify_tx(tx)


def test_bad_hex_and_negative_amount_are_decode_errors(keys):
    _, alice_pub, _ = keys["alice"]
    tx = {"sender_pubkey": "zz", "recipient": keys["carol"],
          "amount": 1, "nonce": 0, "memo": "", "tx_id": "ab" * 32, "signature": "00"}
    with pytest.raises(DecodeError):
        crypto.verify_tx(tx)
    tx2 = {"sender_pubkey": alice_pub, "recipient": keys["carol"],
           "amount": -1, "nonce": 0, "memo": "", "tx_id": "ab" * 32, "signature": "00"}
    with pytest.raises(DecodeError):
        crypto.verify_tx(tx2)


def test_merkle_root_known_values():
    # 空树
    assert crypto.merkle_root([]) == "0" * 64
    # 单元素树：根就是叶子自身（不再哈希）
    leaf = "ab" * 32
    assert crypto.merkle_root([leaf]) == leaf
    # 两元素：根 = sha256d(a || b)，顺序敏感
    a, b = "11" * 32, "22" * 32
    assert crypto.merkle_root([a, b]) == crypto.sha256d(
        bytes.fromhex(a) + bytes.fromhex(b)).hex()
    assert crypto.merkle_root([a, b]) != crypto.merkle_root([b, a])
    # 奇数个节点复制最后一个：[a,b,c] == [a,b,c,c]
    c = "33" * 32
    assert crypto.merkle_root([a, b, c]) == crypto.merkle_root([a, b, c, c])


def test_block_hash_commits_to_parent_and_body(make, keys):
    g = make(crypto.ZERO_HASH, 0, [], 1)
    h = g["header"]
    assert h["block_hash"] == crypto.block_hash_of(h)
    changed = dict(h, prev_hash="ab" * 32)
    assert crypto.block_hash_of(changed) != h["block_hash"]


def test_unsigned_non_genesis_block_rejected(make, keys):
    from reorgindex.errors import VerificationError

    g = make(crypto.ZERO_HASH, 0, [], 1)
    _, alice_pub, alice = keys["alice"]
    body = make_tx(keys["alice"][0], alice_pub, alice, 1, 0)
    b1 = make(g["header"]["block_hash"], 1, [body], 1)
    crypto.verify_block_header(b1["header"], b1["txs"])
    b1["header"]["signature"] = "00" * 64  # 破坏签名
    with pytest.raises(VerificationError):
        crypto.verify_block_header(b1["header"], b1["txs"])


def test_genesis_must_have_zero_parent(make):
    bad = make("ab" * 32, 0, [], 1)
    from reorgindex.errors import ConsensusRuleError

    with pytest.raises(ConsensusRuleError):
        crypto.verify_block_header(bad["header"], bad["txs"])


def test_merkle_mismatch_rejected(make, keys):
    g = make(crypto.ZERO_HASH, 0, [], 1)
    # 手工构造一个交易列表与头中根不一致的块
    _, alice_pub, alice = keys["alice"]
    t1 = make_tx(keys["alice"][0], alice_pub, alice, 1, 0)
    t2 = make_tx(keys["alice"][0], alice_pub, alice, 2, 1)
    header = dict(g["header"])
    header["height"] = 1
    header["prev_hash"] = g["header"]["block_hash"]
    header["merkle_root"] = crypto.merkle_root([t1["tx_id"]])  # 只含 t1
    header["proposer"] = keys["proposer"][1]
    header["block_hash"] = crypto.block_hash_of(header)
    header["signature"] = crypto.sign_block(keys["proposer"][0], header)
    with pytest.raises(VerificationError):
        crypto.verify_block_header(header, [t1, t2])
