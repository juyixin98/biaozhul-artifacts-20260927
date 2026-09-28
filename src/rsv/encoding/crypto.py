"""域标签 + 交易摘要的验签原语（模块一：编码与验签）。

设计要点
- 签名绑定的不是裸交易，而是 ``SHA256(network || domain || canonical_tx)``
  之后再按 RFC 6979/ECDSA(secp256k1) 对 32 字节摘要做 DER 签名。
- *错误交易域*（用 domainA 的交易去花 domainB 的币）会在状态层直接拦截
  （state.domain_conflict）；即便绕过，签名也是在另一条摘要上，验签必然失败
  （compute.crypto.sig），两条失败路径测试都会覆盖。
- 密码学完全委托成熟库 `cryptography`（hazmat），本模块不自实现曲线运算。
"""

from __future__ import annotations

import hashlib

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature, Prehashed

from ..errors import (
    CHECKSIG_FAILED,
    INTERNAL_ERROR,
    MALFORMED_PUBKEY,
    MALFORMED_SIGNATURE,
    VerificationFailure,
)

_CURVE = ec.SECP256K1()


def sha256(b: bytes) -> bytes:
    return hashlib.sha256(b).digest()


def ripemd160(b: bytes) -> bytes:
    h = hashlib.new("ripemd160")
    h.update(b)
    return h.digest()


def hash256(b: bytes) -> bytes:
    return sha256(sha256(b))


def hash160(b: bytes) -> bytes:
    return ripemd160(sha256(b))


def build_message(network: str, domain: str, canonical_tx: bytes) -> bytes:
    """构造绑定网络与域的签名消息，再 hash256 得到 32B 摘要。

    长度前缀统一使用 LEB128（与交易规范化序列化一致，见
    encoding.transaction._leb），保证不同实现间字节级可复现。
    """
    tag = b"rsv-sighash-v1"
    net = network.encode("utf-8")
    dom = domain.encode("utf-8")
    body = (
        tag
        + _leb(len(net)) + net
        + _leb(len(dom)) + dom
        + _leb(len(canonical_tx)) + canonical_tx
    )
    return hash256(body)


def _leb(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def parse_public_key(pub: bytes) -> ec.EllipticCurvePublicKey:
    if not isinstance(pub, (bytes, bytearray)):
        raise VerificationFailure(MALFORMED_PUBKEY, "pubkey must be bytes")
    try:
        loaded = serialization.load_der_public_key(
            _sec1_to_spki(bytes(pub))
        )
    except Exception as exc:  # 各种解析错误归一化
        raise VerificationFailure(MALFORMED_PUBKEY, str(exc)) from exc
    if not isinstance(loaded.curve, ec.SECP256K1):
        raise VerificationFailure(MALFORMED_PUBKEY, "curve is not secp256k1")
    return loaded  # type: ignore[return-value]


def _sec1_to_spki(sec1: bytes) -> bytes:
    """SEC1 点编码（33B 压缩 / 65B 未压缩）包成 SPKI DER 供 cryptography 加载。"""
    if len(sec1) not in (33, 65) or sec1[0] not in (0x02, 0x03, 0x04):
        raise VerificationFailure(
            MALFORMED_PUBKEY,
            f"bad SEC1 prefix/length: 0x{sec1[0]:02X}/{len(sec1)}" if sec1 else "empty pubkey",
        )
    # secp256k1 命名曲线 OID 1.3.132.0.10
    alg = bytes.fromhex("301006072A8648CE3D020106052B8104000A")
    bit_string = b"\x03" + _der_len(len(sec1) + 1) + b"\x00" + sec1
    inner = alg + bit_string
    return b"\x30" + _der_len(len(inner)) + inner


def _der_len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    if n <= 0xFF:
        return bytes([0x81, n])
    if n <= 0xFFFF:
        return bytes([0x82, n >> 8, n & 0xFF])
    return bytes([0x83, (n >> 16) & 0xFF, (n >> 8) & 0xFF, n & 0xFF])


def validate_signature_der(sig: bytes) -> None:
    """严格 DER 检查（两个正整数、总长度一致）。"""
    try:
        r, s = decode_dss_signature(bytes(sig))
    except Exception as exc:
        raise VerificationFailure(MALFORMED_SIGNATURE, str(exc)) from exc
    if not (1 <= r < 1 << 256 and 1 <= s < 1 << 256):
        raise VerificationFailure(MALFORMED_SIGNATURE, "r/s out of range")


def verify_signature(sig: bytes, message32: bytes, pub: bytes) -> None:
    """成功返回 None；签名与消息/公钥不匹配抛 CHECKSIG_FAILED。

    供栈机调用；调用方负责先用 parse_public_key / validate_signature_der
    做结构校验（结构错 -> input.*，这里错 -> compute.crypto.sig）。
    """
    key = parse_public_key(pub)
    validate_signature_der(sig)
    try:
        key.verify(sig, message32, ec.ECDSA(Prehashed(hashes.SHA256())))
    except InvalidSignature:
        raise VerificationFailure(CHECKSIG_FAILED, "ECDSA verify returned false")
    except Exception as exc:  # pragma: no cover - 库内部异常归一化
        raise VerificationFailure(INTERNAL_ERROR, str(exc)) from exc


def cross_check_signature(sig: bytes, message32: bytes, pub: bytes) -> bool:
    """独立交叉校验入口：语义同 verify_signature，但返回 bool 不抛分类异常。

    测试用它（直接调用成熟库）与栈机的 OP_CHECKSIG 结果对拍。
    """
    try:
        key = parse_public_key(pub)
        validate_signature_der(sig)
        key.verify(sig, message32, ec.ECDSA(Prehashed(hashes.SHA256())))
        return True
    except (VerificationFailure, InvalidSignature, ValueError, TypeError):
        return False


def sign_digest(priv: ec.EllipticCurvePrivateKey, message32: bytes) -> bytes:
    """对 32B 摘要做确定性 ECDSA（RFC 6979）签名，输出严格 DER。仅夹具/测试使用。"""
    return priv.sign(message32, ec.ECDSA(Prehashed(hashes.SHA256())))
