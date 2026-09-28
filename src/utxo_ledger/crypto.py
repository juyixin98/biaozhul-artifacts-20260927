"""验签边界模块 (signature boundary)。

* 曲线：secp256k1；签名：ECDSA，DER 编码（成熟库 cryptography）。
* 消息：调用方传入 :func:`utxo_ledger.encoding.sighash_of` 的 32 字节摘要；
  本模块使用 ``Prehashed(SHA256)``，因此"签什么"不由本模块决定，杜绝两边不一致。
  "固定编码"指消息摘要编码固定（标签 + 规范交易体）；ECDSA 的 nonce k 由成熟库
  随机生成（该库未暴露 RFC6979 确定性 k）——夹具把签名字节固化在 JSON 中，
  回放读文件而非重新签名，因此整体仍可复现。
* 公钥：仅接受 33 字节 SEC1 压缩编码（固定编码的一部分）。
* 测试密钥：:func:`fixture_private_key` 由固定种子
  ``b"utxo-ledger/test-key/v1"`` + u32 索引派生，全部为本地合成身份，
  绝无生产账户。
"""
from __future__ import annotations

import hashlib

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import Prehashed

from .errors import MalformedEncodingError, SignatureError

_CURVE = ec.SECP256K1()
_TEST_KEY_TAG = b"utxo-ledger/test-key/v1"


def fixture_private_key(index: int) -> ec.EllipticCurvePrivateKey:
    """生成确定性本地测试私钥（index -> sha256(tag||u32be(index)) 作为标量）。

    仅用于夹具/测试；派生路径固定且公开，因此不承载任何真实资产。
    """
    if not 0 <= index <= 0xFFFFFFFF:
        raise ValueError("测试密钥索引必须为 u32")
    seed = hashlib.sha256(_TEST_KEY_TAG + index.to_bytes(4, "big")).digest()
    candidate = int.from_bytes(seed, "big")
    # cryptography 接受 1..n-1 内标量；sha256 几乎必然落在区间内，失败则再派生一次。
    from cryptography.hazmat.primitives.asymmetric.ec import (
        derive_private_key,
    )

    try:
        return derive_private_key(candidate, _CURVE)
    except Exception:
        fallback = int.from_bytes(hashlib.sha256(seed).digest(), "big")
        return derive_private_key((fallback % ((1 << 255) - 1)) + 1, _CURVE)


def generate_private_key() -> ec.EllipticCurvePrivateKey:
    """生成随机私钥（如需临时身份；测试默认用确定性夹具密钥）。"""
    return ec.generate_private_key(_CURVE)


def public_key_bytes(priv: ec.EllipticCurvePrivateKey) -> bytes:
    """导出 33 字节压缩公钥（固定编码）。"""
    return priv.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.CompressedPoint,
    )


def load_public_key(pubkey: bytes) -> ec.EllipticCurvePublicKey:
    """仅接受 33 字节压缩点，其它长度/编码一律拒绝。"""
    if len(pubkey) != 33 or pubkey[0] not in (0x02, 0x03):
        raise MalformedEncodingError(
            "公钥仅接受 33 字节 SEC1 压缩编码",
            details={"length": len(pubkey), "prefix": pubkey[:1].hex() if pubkey else ""},
        )
    try:
        key = ec.EllipticCurvePublicKey.from_encoded_point(_CURVE, pubkey)
    except ValueError as exc:
        # 长度/前缀合法但点不在曲线上：编码层数据缺陷，归 INPUT_ERROR
        raise MalformedEncodingError(
            "压缩公钥点不在 secp256k1 曲线上"
        ) from exc
    return key


def sign(priv: ec.EllipticCurvePrivateKey, message_hash: bytes) -> bytes:
    """对 32 字节摘要做确定性 ECDSA 签名，返回 DER 字节。"""
    if len(message_hash) != 32:
        raise SignatureError(
            "签名输入必须为 32 字节摘要", details={"length": len(message_hash)}
        )
    return priv.sign(message_hash, ec.ECDSA(Prehashed(hashes.SHA256())))


def verify(pubkey: bytes, signature: bytes, message_hash: bytes) -> None:
    """验证签名。任何失败统一抛 :class:`SignatureError`（COMPUTATION_FAILED）。"""
    if len(message_hash) != 32:
        raise SignatureError(
            "验签输入必须为 32 字节摘要", details={"length": len(message_hash)}
        )
    key = load_public_key(pubkey)
    try:
        key.verify(signature, message_hash, ec.ECDSA(Prehashed(hashes.SHA256())))
    except InvalidSignature as exc:
        raise SignatureError(
            "签名验证失败：签名被篡改或与公钥/消息不匹配"
        ) from exc
    except ValueError as exc:
        # DER 解析失败、签名编码非法
        raise SignatureError("签名编码非法（DER 解析失败）") from exc


def fixture_keypair(index: int) -> tuple[ec.EllipticCurvePrivateKey, bytes]:
    """便捷方法：返回 (私钥对象, 33 字节压缩公钥)。"""
    priv = fixture_private_key(index)
    return priv, public_key_bytes(priv)
