"""密码学验签交叉验证：两个独立实现互验。

- 被测核心：cryptography（OpenSSL），见 stackvm.crypto；
- 参考实现：ecdsa（纯 Python），用它独立签名**并独立验证**。
两组测试互为镜像，任何一方出错都会被另一方抓到。
"""
from __future__ import annotations

import hashlib
import json

import ecdsa
import pytest
from ecdsa import SECP256k1, BadSignatureError, SigningKey

from stackvm import crypto
from stackvm.config import PROJECT_ROOT
from stackvm.errors import FailCode, VmFailure

KEYS = json.loads((PROJECT_ROOT / "fixtures" / "keys.json").read_text("utf-8"))["keys"]


def _sk(name: str) -> SigningKey:
    return SigningKey.from_string(bytes.fromhex(KEYS[name]["priv_hex"]), curve=SECP256k1)


def _pub(name: str) -> bytes:
    return bytes.fromhex(KEYS[name]["pub_hex"])


def _ecdsa_verify(pub: bytes, sig: bytes, digest: bytes) -> bool:
    # hashfunc=sha256：库默认 SHA-1；sigdecode_der：verify_digest 默认按裸 64 字节解码
    vk = ecdsa.VerifyingKey.from_string(
        pub, curve=SECP256k1, hashfunc=hashlib.sha256,
        valid_encodings=("compressed",))
    try:
        return vk.verify_digest(sig, digest, sigdecode=ecdsa.util.sigdecode_der)
    except BadSignatureError:
        return False


@pytest.mark.parametrize("name", ["alice", "bob", "carol", "dave"])
def test_fixture_pubkeys_load_in_crypto(name):
    # 夹具公钥由 ecdsa 派生；cryptography 必须能解析为合法曲线点
    key = crypto.parse_pubkey(_pub(name))
    assert key.key_size == 256


def _low_s(der: bytes) -> bytes:
    r, s = ecdsa.util.sigdecode_der(der, SECP256k1.order)
    if s > SECP256k1.order // 2:
        s = SECP256k1.order - s
    return ecdsa.util.sigencode_der(r, s, SECP256k1.order)


@pytest.mark.parametrize("name", ["alice", "bob", "carol", "dave"])
def test_crossverify_ecdsa_signed_crypto_verifies(name):
    sk = _sk(name)
    digest = hashlib.sha256(f"msg-{name}".encode()).digest()
    raw = sk.sign_digest_deterministic(digest, hashfunc=hashlib.sha256,
                                       sigencode=ecdsa.util.sigencode_der)
    # 协议要求 low-S：规范化后两库都必须接受
    sig = _low_s(raw)
    assert _ecdsa_verify(_pub(name), sig, digest)
    assert crypto.verify(_pub(name), sig, digest) is True
    # 若该摘要自然产生 high-S，原始签名必须被被测核心拒绝（策略比参考库更严）
    if raw != sig:
        with pytest.raises(VmFailure) as ei:
            crypto.parse_signature(raw)
        assert ei.value.code is FailCode.SIG_INVALID


@pytest.mark.parametrize("name", ["alice", "bob", "carol", "dave"])
def test_crossverify_crypto_signed_ecdsa_verifies(name):
    """反向：用 cryptography 签名，ecdsa 独立验证（经 stackvm 的解析+校验路径）。"""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import Prehashed

    scalar = int.from_bytes(bytes.fromhex(KEYS[name]["priv_hex"]), "big")
    priv = ec.derive_private_key(scalar, ec.SECP256K1())
    digest = hashlib.sha256(f"reverse-{name}".encode()).digest()
    raw = priv.sign(digest, ec.ECDSA(Prehashed(hashes.SHA256())))
    sig = _low_s(raw)
    # 被测核心验过（协议要求 low-S）
    assert crypto.verify(_pub(name), sig, digest) is True
    # 独立 ecdsa 库同意
    assert _ecdsa_verify(_pub(name), sig, digest) is True

    # 公钥序列化一致性：cryptography 导出的压缩点必须与夹具字节相同
    exported = priv.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.CompressedPoint)
    assert exported == _pub(name)


def test_wrong_digest_rejected_by_both():
    digest = hashlib.sha256(b"right").digest()
    other = hashlib.sha256(b"wrong").digest()
    raw = _sk("alice").sign_digest_deterministic(
        digest, hashfunc=hashlib.sha256, sigencode=ecdsa.util.sigencode_der)
    sig = _low_s(raw)
    assert crypto.verify(_pub("alice"), sig, other) is False
    assert _ecdsa_verify(_pub("alice"), sig, other) is False


def test_wrong_pubkey_rejected():
    digest = hashlib.sha256(b"x").digest()
    raw = _sk("alice").sign_digest_deterministic(
        digest, hashfunc=hashlib.sha256, sigencode=ecdsa.util.sigencode_der)
    sig = _low_s(raw)
    assert crypto.verify(_pub("bob"), sig, digest) is False


def test_bit_flipped_signature_rejected():
    digest = hashlib.sha256(b"x").digest()
    sig = bytearray(_sk("carol").sign_digest_deterministic(
        digest, hashfunc=hashlib.sha256, sigencode=ecdsa.util.sigencode_der))
    sig[-1] ^= 0x01
    # 翻转后可能非法 DER（SIG_INVALID 结构错）或只是密码不匹配（False），
    # 两种情况都必须不通过；这里只断言不会被接受。
    try:
        accepted = crypto.verify(_pub("carol"), bytes(sig), digest)
    except VmFailure:
        accepted = False
    assert accepted is False


def test_high_s_signature_rejected_by_core():
    """high-S（第三值）签名：本协议的严格 low-S 策略必须拒绝。

    注：ecdsa 参考库默认接受 high-S（其延展性策略需另行开启），所以本用例
    只对被测核心断言；low-S 规范化后两库一致性由其它用例覆盖。
    """
    digest = hashlib.sha256(b"highs").digest()
    raw = _sk("bob").sign_digest_deterministic(
        digest, hashfunc=hashlib.sha256, sigencode=ecdsa.util.sigencode_der)
    r, s = ecdsa.util.sigdecode_der(raw, SECP256k1.order)
    if s <= SECP256k1.order // 2:
        s = SECP256k1.order - s  # 人为构造 high-S 第三值
    high = ecdsa.util.sigencode_der(r, s, SECP256k1.order)
    with pytest.raises(VmFailure) as ei:
        crypto.parse_signature(high)
    assert ei.value.code is FailCode.SIG_INVALID
    # 规范化为 low-S 后即成为可验证的有效签名
    assert crypto.verify(_pub("bob"), _low_s(high), digest) is True


def test_malformed_pubkey_structures():
    good = _pub("alice")
    for bad in [b"", b"\x02" + b"\x00" * 31, b"\x04" + b"\x00" * 32,
                b"\x02" + b"\x00" * 32, good[:-1], good + b"\x00"]:
        with pytest.raises(VmFailure) as ei:
            crypto.parse_pubkey(bad)
        assert ei.value.code is FailCode.SIG_INVALID


def test_malformed_signature_structures():
    digest = hashlib.sha256(b"x").digest()
    for bad in [b"", b"\x00", b"\x30\x00", b"\x30\x06\x02\x01\x00\x02\x01\x00"]:
        with pytest.raises(VmFailure) as ei:
            crypto.parse_signature(bad)
        assert ei.value.code is FailCode.SIG_INVALID
    # 非 32 字节摘要必须直接 SIG_INVALID
    with pytest.raises(VmFailure) as ei:
        crypto.verify(_pub("alice"), b"\x30\x06\x02\x01\x01\x02\x01\x01", b"")
    assert ei.value.code is FailCode.SIG_INVALID


def test_rfc6979_determinism():
    """同一私钥+同一摘要的确定性签名必须逐字节稳定（RFC6979）。"""
    digest = hashlib.sha256(b"det").digest()
    s1 = _sk("dave").sign_digest_deterministic(
        digest, hashfunc=hashlib.sha256, sigencode=ecdsa.util.sigencode_der)
    s2 = _sk("dave").sign_digest_deterministic(
        digest, hashfunc=hashlib.sha256, sigencode=ecdsa.util.sigencode_der)
    assert s1 == s2
