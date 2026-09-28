"""编码与密码学：规范化序列化、双 SHA256、默克尔根、Ed25519 签名/验签。

本模块只有纯函数，不持有任何链状态；内核负责把它们组合成规则。
所有哈希使用 sha256(sha256(x))（双 SHA256），与合成链模型保持一致。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

ZERO_HASH = "0" * 64
ZERO_ROOT = "0" * 64


def sha256d(data: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def canonical_json(obj: Any) -> str:
    """确定性 JSON：键排序、无多余空白。字节以 hex 表示。"""

    def default(o):
        if isinstance(o, bytes):
            return o.hex()
        raise TypeError(f"不可规范化的类型: {type(o)!r}")

    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=default)


def digest_object(obj: Any) -> bytes:
    return sha256d(canonical_json(obj).encode("utf-8"))


# ---------------------------------------------------------------- 交易

# 参与签名/交易号的字段（固定顺序无关，规范化时按键排序）。
TX_BODY_FIELDS = ("sender_pubkey", "recipient", "amount", "nonce", "memo")


def address_of_pubkey(pubkey_hex: str) -> str:
    """地址 = sha256(公钥) 的十六进制，32 字节。"""

    return hashlib.sha256(bytes.fromhex(pubkey_hex)).hexdigest()


def tx_body(tx: dict) -> dict:
    return {k: tx.get(k, 0 if k in ("amount", "nonce") else "") for k in TX_BODY_FIELDS}


def tx_id_of(tx: dict) -> str:
    """交易号 = sha256d(规范化交易体)。交易体不含 tx_id 与 signature。"""

    return digest_object(tx_body(tx)).hex()


def tx_signing_message(tx: dict) -> bytes:
    return digest_object({"domain": "reorgindex.tx/v1", "body": tx_body(tx)})


def sign_tx(private_key: Ed25519PrivateKey, tx: dict) -> str:
    return private_key.sign(tx_signing_message(tx)).hex()


def verify_tx(tx: dict) -> None:
    """完整校验交易；失败抛 VerificationError / DecodeError。"""

    from .errors import DecodeError, VerificationError

    body = tx_body(tx)
    for hex_field in ("sender_pubkey", "recipient"):
        try:
            raw = bytes.fromhex(body[hex_field])
        except (ValueError, TypeError) as exc:
            raise DecodeError(f"{hex_field} 不是合法十六进制", field=hex_field) from exc
        if len(raw) != 32:
            raise DecodeError(f"{hex_field} 必须是 32 字节", field=hex_field, length=len(raw))
    if not isinstance(body["amount"], int) or isinstance(body["amount"], bool) or body["amount"] < 0:
        raise DecodeError("amount 必须是非负整数", amount=body["amount"])
    if not isinstance(body["nonce"], int) or isinstance(body["nonce"], bool) or body["nonce"] < 0:
        raise DecodeError("nonce 必须是非负整数", nonce=body["nonce"])
    if tx.get("tx_id") != tx_id_of(body):
        raise VerificationError(
            "tx_id 与交易体不一致", tx_id=tx.get("tx_id"), expected=tx_id_of(body)
        )
    try:
        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(body["sender_pubkey"]))
        pub.verify(bytes.fromhex(tx["signature"]), tx_signing_message(body))
    except (InvalidSignature, ValueError, KeyError, TypeError) as exc:
        raise VerificationError("交易签名验证失败", tx_id=tx.get("tx_id")) from exc


def generate_key() -> tuple[Ed25519PrivateKey, str]:
    """生成测试/夹具用密钥，返回（私钥对象, 公钥 hex）。"""

    priv = Ed25519PrivateKey.generate()
    return priv, priv.public_key().public_bytes_raw().hex()


# ---------------------------------------------------------------- 默克尔树

def merkle_root(tx_ids: Iterable[str]) -> str:
    """两两配对 sha256d，奇数节点复制自身；空列表返回 32 个 0。"""

    level = [bytes.fromhex(t) for t in tx_ids]
    if not level:
        return ZERO_ROOT
    while len(level) > 1:
        if len(level) % 2 == 1:
            level.append(level[-1])
        level = [sha256d(level[i] + level[i + 1]) for i in range(0, len(level), 2)]
    return level[0].hex()


# ---------------------------------------------------------------- 区块头

BLOCK_VERSION = 1


def block_header_payload(header: dict) -> dict:
    return {
        "domain": "reorgindex.block/v1",
        "version": header.get("version", BLOCK_VERSION),
        "prev_hash": header["prev_hash"],
        "height": header["height"],
        "merkle_root": header["merkle_root"],
        "weight": header["weight"],
        "proposer": header.get("proposer", ""),
    }


def block_hash_of(header: dict) -> str:
    return digest_object(block_header_payload(header)).hex()


def block_signing_message(header: dict) -> bytes:
    return digest_object({"signed_over": "reorgindex.block/v1", "block_hash": block_hash_of(header)})


def sign_block(private_key: Ed25519PrivateKey, header: dict) -> str:
    return private_key.sign(block_signing_message(header)).hex()


def verify_block_header(header: dict, txs: list[dict]) -> None:
    """校验区块头：哈希、高度、权重、默克尔根、签名。不校验父连接（内核负责）。"""

    from .errors import ConsensusRuleError, DecodeError, VerificationError

    height = header.get("height")
    if not isinstance(height, int) or isinstance(height, bool) or height < 0:
        raise DecodeError("height 必须是非负整数", height=height)
    weight = header.get("weight")
    if not isinstance(weight, int) or isinstance(weight, bool) or weight <= 0:
        raise ConsensusRuleError("区块权重必须为正整数", weight=weight)
    if not isinstance(header.get("prev_hash"), str) or len(header["prev_hash"]) != 64:
        raise DecodeError("prev_hash 必须是 64 位十六进制")
    try:
        bytes.fromhex(header["prev_hash"])
    except ValueError as exc:
        raise DecodeError("prev_hash 不是合法十六进制") from exc
    if header.get("block_hash") != block_hash_of(header):
        raise VerificationError(
            "block_hash 与区块头不一致",
            block_hash=header.get("block_hash"),
            expected=block_hash_of(header),
        )
    expected_root = merkle_root([t["tx_id"] for t in txs])
    if header.get("merkle_root") != expected_root:
        raise VerificationError(
            "merkle_root 与交易列表不一致",
            merkle_root=header.get("merkle_root"),
            expected=expected_root,
        )
    # 创世块免签名（本地合成约定）；其余区块必须携带提议者对区块哈希的有效签名。
    if height == 0:
        if header["prev_hash"] != ZERO_HASH:
            raise ConsensusRuleError("创世块的 prev_hash 必须为全零", height=height)
        return
    proposer = header.get("proposer", "")
    signature = header.get("signature", "")
    try:
        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(proposer))
        pub.verify(bytes.fromhex(signature), block_signing_message(header))
    except (InvalidSignature, ValueError, TypeError) as exc:
        raise VerificationError("区块签名验证失败", block_hash=header.get("block_hash")) from exc
