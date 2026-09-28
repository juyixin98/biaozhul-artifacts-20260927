"""Ed25519 签名/验签与地址派生测试。"""

from __future__ import annotations

import base64

import pytest

from cryptography.hazmat.primitives import serialization

from teachchain import crypto, fixtures
from teachchain.errors import Rejected
from teachchain.models import verify_envelope


def test_address_deterministic_from_pubkey(alice):
    addr = crypto.address_from_public_key(alice.key.public_key())
    assert addr == alice.address
    assert addr.startswith("0x") and len(addr) == 18
    # 相同种子每次派生同一地址
    assert fixtures.signer("alice").address == fixtures.signer("alice").address
    assert fixtures.signer("alice").address != fixtures.signer("bob").address


def test_sign_and_verify_roundtrip(alice):
    digest = crypto.sha256_hex(b"hello")
    sig = alice.sign_digest_b64(digest)
    raw_pub = alice.key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    assert len(raw_pub) == 32
    pem = crypto.public_key_to_pem(alice.key.public_key())
    assert crypto.verify_signature_b64(pem, digest, sig)
    # 改一个摘要位即验签失败
    bad = "0" * 64 if digest != "0" * 64 else "1" * 64
    assert not crypto.verify_signature_b64(pem, bad, sig)


def test_verify_envelope_accepts_valid(alice):
    env = fixtures.envelope(alice, "deploy", nonce=0, gas_limit=100_000,
                            code=b"\x00")
    body, tx_hash = verify_envelope(env)
    assert body["from"] == alice.address
    assert len(tx_hash) == 64


def test_verify_envelope_rejects_tampered_sig(alice):
    env = fixtures.envelope(alice, "deploy", nonce=0, gas_limit=100_000,
                            code=b"\x00")
    raw = base64.b64decode(env["sig_b64"])
    raw = bytes([raw[0] ^ 1]) + raw[1:]
    env["sig_b64"] = base64.b64encode(raw).decode()
    with pytest.raises(Rejected) as ei:
        verify_envelope(env)
    assert ei.value.code == "bad_signature"


def test_verify_envelope_rejects_wrong_pubkey_length(alice):
    env = fixtures.envelope(alice, "deploy", nonce=0, gas_limit=100_000,
                            code=b"\x00")
    env["pub_b64"] = base64.b64encode(b"\x00" * 31).decode()
    with pytest.raises(Rejected) as ei:
        verify_envelope(env)
    assert ei.value.code == "bad_signature"


def test_verify_envelope_rejects_sender_mismatch(alice, bob):
    env = fixtures.envelope(alice, "deploy", nonce=0, gas_limit=100_000,
                            code=b"\x00")
    env["tx"]["from"] = bob.address
    with pytest.raises(Rejected) as ei:
        verify_envelope(env)
    # 篡改 from 后签名失效或 sender_mismatch
    assert ei.value.code in ("bad_signature", "sender_mismatch")


def test_canonical_json_deterministic():
    a = crypto.canonical_json({"z": 1, "a": [3, 2, 1], "n": None})
    b = crypto.canonical_json({"a": [3, 2, 1], "n": None, "z": 1})
    assert a == b
    assert a == b'{"a":[3,2,1],"n":null,"z":1}'


def test_digest_binds_all_fields(alice):
    e1 = fixtures.envelope(alice, "invoke", nonce=0, gas_limit=100,
                           to="0x" + "ab" * 8, words=[1])
    e2 = fixtures.envelope(alice, "invoke", nonce=0, gas_limit=100,
                           to="0x" + "ab" * 8, words=[2])
    _, h1 = verify_envelope(e1)
    _, h2 = verify_envelope(e2)
    assert h1 != h2  # 输入不同 -> 摘要不同
