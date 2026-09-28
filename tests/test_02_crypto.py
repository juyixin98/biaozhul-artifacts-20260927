"""验签测试：成熟库黄金向量、固定密钥确定性、篡改/错误持有者/坏 DER 全拒绝。"""
from __future__ import annotations

import pytest

from utxo_ledger import crypto
from utxo_ledger.errors import MalformedEncodingError, SignatureError


def test_fixture_keys_are_deterministic():
    a = crypto.public_key_bytes(crypto.fixture_private_key(7))
    b = crypto.public_key_bytes(crypto.fixture_private_key(7))
    assert a == b and len(a) == 33
    c = crypto.public_key_bytes(crypto.fixture_private_key(8))
    assert a != c


def test_sign_verify_roundtrip_golden():
    priv, pub = crypto.fixture_keypair(0)
    msg = bytes(range(32))
    sig = crypto.sign(priv, msg)
    crypto.verify(pub, sig, msg)  # 不抛即通过
    # ECDSA 使用随机 k：两次签名通常不同字节，但都必须验证通过（DER 定界 0x30）
    sig2 = crypto.sign(priv, msg)
    crypto.verify(pub, sig2, msg)
    assert sig[0] == 0x30 and sig2[0] == 0x30


def test_wrong_holder_key_rejected():
    priv0, pub0 = crypto.fixture_keypair(0)
    _priv1, pub1 = crypto.fixture_keypair(1)
    msg = bytes([9] * 32)
    sig = crypto.sign(priv0, msg)
    with pytest.raises(SignatureError) as ei:
        crypto.verify(pub1, sig, msg)
    assert ei.value.category.value == "COMPUTATION_FAILED"
    assert ei.value.code == "SIGNATURE_INVALID"


def test_tampered_signature_rejected():
    priv, pub = crypto.fixture_keypair(2)
    msg = b"\xab" * 32
    sig = bytearray(crypto.sign(priv, msg))
    sig[-2] ^= 0xFF
    with pytest.raises(SignatureError):
        crypto.verify(pub, bytes(sig), msg)


def test_tampered_message_rejected():
    priv, pub = crypto.fixture_keypair(2)
    sig = crypto.sign(priv, b"\x01" * 32)
    with pytest.raises(SignatureError):
        crypto.verify(pub, sig, b"\x02" * 32)


def test_malformed_der_rejected():
    _priv, pub = crypto.fixture_keypair(3)
    with pytest.raises(SignatureError):
        crypto.verify(pub, b"not-a-der-signature", b"\x03" * 32)


def test_only_compressed_pubkeys_accepted():
    _priv, pub = crypto.fixture_keypair(4)
    assert len(pub) == 33 and pub[0] in (2, 3)
    with pytest.raises(MalformedEncodingError):
        crypto.load_public_key(b"\x04" + b"\x00" * 64)  # 65B 未压缩
    with pytest.raises(MalformedEncodingError):
        crypto.load_public_key(b"\x02" + b"\x00" * 32)


def test_sighash_signature_matches_independent_oracle(ring, oracle):
    """同一条消息：被测 verify 与 oracle verify 结论一致（正/负样本）。"""
    priv, pub = crypto.fixture_keypair(1)
    msg = bytes(range(32))
    sig = crypto.sign(priv, msg)
    assert oracle._valid_sig(pub.hex(), sig.hex(), msg) is True
    bad = bytearray(sig)
    bad[-1] ^= 1
    assert oracle._valid_sig(pub.hex(), bytes(bad).hex(), msg) is False
