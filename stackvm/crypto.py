"""ECDSA(SECP256K1) 验签 —— 基于成熟密码库 `cryptography`（OpenSSL 后端）。

约定（受限协议的一部分，与 Bitcoin 脚本格式相似但不完全相同）：
- 公钥：SEC1 压缩点，恰好 33 字节（0x02/0x03 前缀）。
- 签名：DER 编码的 ECDSA-Sig-Value（r,s）。非法 DER / 非曲线点一律 SIG_INVALID，
  不做“可补救”签名的宽松化处理。
- 待验消息：32 字节交易摘要（sighash，已 SHA-256 预哈希，用 Prehashed 验签）。
- 编码/曲线结构异常收敛为 SIG_INVALID；密码学不匹配（签名值错误）返回 False，
  由 VM 映射到 SIG_INVALID / THRESHOLD_NOT_MET。
"""
from __future__ import annotations

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import Prehashed, decode_dss_signature

from .errors import FailCode, VmFailure

PUBKEY_LEN = 33
DIGEST_LEN = 32

# secp256k1 群阶 n（公开曲线常量）
SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141

# secp256k1 命名曲线 OID 1.3.132.0.10 的 AlgorithmIdentifier
_SECP256K1_ALGORITHM = bytes.fromhex("301006072a8648ce3d020106052b8104000a")


def _tlv(tag: int, content: bytes) -> bytes:
    n = len(content)
    if n < 0x80:
        length = bytes([n])
    elif n <= 0xFF:
        length = bytes([0x81, n])
    else:
        length = bytes([0x82, n >> 8, n & 0xFF])
    return bytes([tag]) + length + content


def _spki(compressed_point: bytes) -> bytes:
    return _tlv(
        0x30,
        _SECP256K1_ALGORITHM + _tlv(0x03, b"\x00" + compressed_point),
    )


def parse_pubkey(pub: bytes) -> ec.EllipticCurvePublicKey:
    """解析压缩 SECP256K1 公钥；结构性非法 → SIG_INVALID。"""
    if not isinstance(pub, (bytes, bytearray)) or len(pub) != PUBKEY_LEN:
        actual = len(pub) if isinstance(pub, (bytes, bytearray)) else type(pub).__name__
        raise VmFailure(FailCode.SIG_INVALID, f"公钥必须为 {PUBKEY_LEN} 字节压缩格式，实际 {actual}")
    if pub[0] not in (0x02, 0x03):
        raise VmFailure(FailCode.SIG_INVALID, "公钥前缀必须为 0x02 或 0x03")
    try:
        loaded = serialization.load_der_public_key(_spki(bytes(pub)))
    except Exception:  # noqa: BLE001 - 任何点/编码异常收敛
        raise VmFailure(FailCode.SIG_INVALID, "公钥不是合法 secp256k1 曲线点") from None
    return loaded


def parse_signature(sig: bytes) -> tuple[int, int]:
    """解析 DER ECDSA 签名；结构性非法或 high-S → SIG_INVALID。

    严格要求 low-S（s ≤ n/2）：与 Bitcoin 的低 S 规则一致，消除第三值延展性，
    也让本项目的验签结果与独立 ecdsa 参考库（默认拒绝 high-S）保持一致。
    """
    if not isinstance(sig, (bytes, bytearray)) or not sig:
        raise VmFailure(FailCode.SIG_INVALID, "签名必须为非空 DER 字节序列")
    try:
        r, s = decode_dss_signature(bytes(sig))
    except Exception:  # noqa: BLE001
        raise VmFailure(FailCode.SIG_INVALID, "签名不是合法 DER ECDSA 编码") from None
    if r == 0 or s == 0:
        raise VmFailure(FailCode.SIG_INVALID, "签名分量为零")
    if r >= SECP256K1_N or s >= SECP256K1_N:
        raise VmFailure(FailCode.SIG_INVALID, "签名分量超出曲线群阶")
    if s > SECP256K1_N // 2:
        raise VmFailure(
            FailCode.SIG_INVALID,
            "签名 s 值不在 low-S 范围（s > n/2），拒绝高 S 签名以防延展性")
    return r, s


def verify(pub: bytes, sig: bytes, digest: bytes) -> bool:
    """验签。结构非法抛 SIG_INVALID；密码不匹配返回 False；匹配返回 True。"""
    if not isinstance(digest, (bytes, bytearray)) or len(digest) != DIGEST_LEN:
        raise VmFailure(FailCode.SIG_INVALID, f"交易摘要必须为 {DIGEST_LEN} 字节")
    key = parse_pubkey(pub)
    parse_signature(sig)
    try:
        key.verify(bytes(sig), bytes(digest), ec.ECDSA(Prehashed(hashes.SHA256())))
    except InvalidSignature:
        return False
    except Exception:  # noqa: BLE001 - 结构异常理论上已被上面的预解析覆盖
        return False
    return True
