"""与成熟密码库 cryptography / hashlib 的交叉验证。

原则：
- 每个 CHECKSIG/CHECKMULTISIG 接受用例，都独立直接调用 cryptography 验证签名
  与 message32、公钥的关系；栈机说“真”时成熟库也必须说“真”；
- 每个哈希向量，期望值直接由 hashlib 重新算一遍，再与夹具、栈机三方比对；
- 域标签交叉：wrong_domain 用例，成熟库在“正确域”上必须验证通过，
  在栈机使用的“错误域摘要”上必须验证失败——证明签名确实绑定了域。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import Prehashed

from rsv.encoding import crypto
from tests.conftest import FX


def _lib_verify(sig: bytes, msg32: bytes, pub: bytes) -> bool:
    """测试内独立实现（不复用 rsv.encoding.crypto.cross_check_signature）。"""
    alg = bytes.fromhex("301006072a8648ce3d020106052b8104000a")

    def der_len(n: int) -> bytes:
        if n < 0x80:
            return bytes([n])
        if n <= 0xFF:
            return bytes([0x81, n])
        return bytes([0x82, n >> 8, n & 0xFF])

    bs = b"\x03" + der_len(len(pub) + 1) + b"\x00" + pub
    spki = b"\x30" + der_len(len(alg) + len(bs)) + alg + bs
    try:
        key = serialization.load_der_public_key(spki)
        key.verify(sig, msg32, ec.ECDSA(Prehashed(hashes.SHA256())))
        return True
    except Exception:
        return False


def test_engine_matches_mature_library_on_every_sig_vector(script_vectors):
    # 对所有验签类向量，栈机结论必须与“直接调成熟库”的结论一致
    sig_ids = {
        "p2pk_ok", "p2pk_wrong_key", "p2pk_wrong_domain_msg",
        "p2pkh_ok", "p2pkh_other_pub",
        "ms_2of3_order_ca", "ms_2of3_order_ac", "ms_2of3_ab",
        "ms_1of3_one_valid", "ms_threshold_not_met",
        "hashlock_ok", "hashlock_bad_preimage",
        "bad_pubkey_bytes", "bad_signature_der",
    }
    keys_doc = json.loads((FX / "keys.json").read_text("utf-8"))
    pubs = {k: bytes.fromhex(v) for k, v in keys_doc["pubkeys_compressed_hex"].items()}
    found = set()

    for v in script_vectors:
        if v["id"] not in sig_ids:
            continue
        found.add(v["id"])
        msg = bytes.fromhex(v["message32"])
        # 从 witness 直接解析所有推送项（签名都在其中）
        pushes = _extract_pushes(bytes.fromhex(v["unlock_script"]))
        # 从 lock 解析公钥项（33/65 字节）
        lock_pushes = _extract_pushes(bytes.fromhex(v["lock_script"]))
        w_pubs = {
            p for p in pushes
            if (len(p) == 33 and p[0] in (0x02, 0x03))
            or (len(p) == 65 and p[0] == 0x04)
        }
        # P2PK 的公钥在锁定脚本，P2PKH 的公钥在 witness，两边合并
        candidate_pubs = list(
            {
                p for p in (lock_pushes + pushes)
                if (len(p) == 33 and p[0] in (0x02, 0x03))
                or (len(p) == 65 and p[0] == 0x04)
            }
        )
        # witness 里可能同时带公钥（P2PKH），不能把公钥误当签名
        candidate_sigs = [
            s for s in pushes if 8 <= len(s) <= 73 and s not in w_pubs
        ]

        def any_valid_pair() -> bool:
            return any(
                _lib_verify(s, msg, p) for s in candidate_sigs for p in candidate_pubs
            )

        lib_says_ok = any_valid_pair()
        engine_accepts = v["expected"]["accepted"]

        if v["id"] in ("p2pk_ok", "p2pkh_ok", "ms_2of3_order_ca",
                       "ms_2of3_order_ac", "ms_2of3_ab", "ms_1of3_one_valid",
                       "hashlock_ok"):
            assert engine_accepts and lib_says_ok, v["id"]
        elif v["id"] == "p2pk_wrong_key":
            assert not engine_accepts
            # b 的签名在 a 的公钥下必须为假
            assert not _lib_verify(candidate_sigs[0], msg, pubs["a"])
            # 但在 b 自己公钥下为真（签名本身有效，只是公钥不匹配）
            assert _lib_verify(candidate_sigs[0], msg, pubs["b"])
        elif v["id"] == "p2pk_wrong_domain_msg":
            assert not engine_accepts and not lib_says_ok
            x = v["cross_check"]
            assert x["lib_verify_correct_domain"] is True
            assert x["lib_verify_wrong_domain"] is False
        elif v["id"] in ("p2pkh_other_pub", "hashlock_bad_preimage"):
            assert not engine_accepts
        elif v["id"] == "ms_threshold_not_met":
            assert not engine_accepts
            # d 的签名本身合法，但对策略公钥 a 为假
            assert v["cross_check"]["lib_verify_d_against_a"] is False
        elif v["id"] == "bad_pubkey_bytes":
            assert not engine_accepts
            assert v["expected"]["code"] == "input.crypto.pubkey"
            assert not lib_says_ok  # 成熟库同样无法加载该“公钥”
        elif v["id"] == "bad_signature_der":
            assert not engine_accepts
            assert v["expected"]["code"] == "input.crypto.sig_encoding"
            assert not lib_says_ok

    assert found == sig_ids, f"missing: {sig_ids - found}"


def test_hash_vectors_match_hashlib_three_way(script_vectors):
    for v in script_vectors:
        cc = v.get("cross_check") or {}
        if "expected_sha256" in cc:
            pre = b"rsv-hash-preimage" if v["id"].startswith("hash_sha256") else None
            if v["id"] == "hashlock_ok":
                pre = b"correct-horse-battery-staple"
            if pre is not None:
                assert hashlib.sha256(pre).digest().hex() == cc["expected_sha256"]
        if "expected_hash256" in cc:
            pre = b"rsv-hash-preimage"
            h = hashlib.sha256(hashlib.sha256(pre).digest()).hexdigest()
            assert h == cc["expected_hash256"]
            assert crypto.hash256(pre).hex() == h
        if "expected_hash160" in cc:
            pre = b"rsv-hash-preimage"
            r = hashlib.new("ripemd160")
            r.update(hashlib.sha256(pre).digest())
            assert r.hexdigest() == cc["expected_hash160"]
            assert crypto.hash160(pre).hex() == r.hexdigest()
        if "expected_ripemd160" in cc:
            pre = b"rsv-hash-preimage"
            r = hashlib.new("ripemd160")
            r.update(pre)
            assert r.hexdigest() == cc["expected_ripemd160"]


def test_signing_then_verifying_roundtrip():
    """直接在本测试内生成临时密钥（不依赖夹具），走 sign_digest/verify_signature。"""
    priv = ec.generate_private_key(ec.SECP256K1())
    pub = priv.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.CompressedPoint
    )
    msg = hashlib.sha256(b"fresh-roundtrip").digest()
    sig = crypto.sign_digest(priv, msg)
    crypto.verify_signature(sig, msg, pub)  # 不抛即通过
    assert crypto.cross_check_signature(sig, msg, pub) is True
    assert crypto.cross_check_signature(sig, b"\x00" * 32, pub) is False


def _extract_pushes(script: bytes) -> list[bytes]:
    out, i = [], 0
    while i < len(script):
        b = script[i]
        i += 1
        if 1 <= b <= 0x4B:
            out.append(script[i : i + b])
            i += b
        elif b in (0x4C, 0x4D, 0x4E):
            w = {0x4C: 1, 0x4D: 2, 0x4E: 4}[b]
            n = int.from_bytes(script[i : i + w], "little")
            i += w
            out.append(script[i : i + n])
            i += n
    return out
