"""交易编码与验签（模块职责：密码学，不触碰池/链状态）。

设计说明
--------
* 交易格式为类 Ethereum 遗产交易的 RLP 列表::

      [nonce, gas_price, gas_limit, to, value, data, v, r, s]

  签名采用 EIP-155 域分离：待签名消息为
  ``keccak256(rlp([nonce, gas_price, gas_limit, to, value, data, chain_id, 0, 0]))``。
  这样本地合成链（chain_id=31337）上签的交易无法在其它链语境重放。
* 地址 = 最后 20 字节 ``keccak256(secp256k1 公钥)``，与 EVM 一致，便于复核。
* 签名强制 EIP-2 低 s（eth-keys 后端默认）且 v 必须为 EIP-155 两个合法值，
  拒绝第三态可塑性（malleability）。
* 依赖成熟密码库：``coincurve``/``eth-keys``（secp256k1）、``eth-utils``（keccak）、
  ``rlp``（标准编码）。本模块只做薄封装，不自行实现曲线运算。
"""

from __future__ import annotations

import secrets

import rlp
from eth_hash.auto import keccak
from eth_keys import keys
from eth_keys.exceptions import BadSignature
from eth_utils import (
    decode_hex,
    encode_hex,
    is_0x_prefixed,
    is_address,
    to_checksum_address,
    to_normalized_address,
)

from .config import LOCAL_CHAIN_ID
from .models import ErrorCode, Transaction, TxError

# secp256k1 曲线阶，用于拒绝高 s。
SECPK1_N = (
    0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
)


# --------------------------------------------------------------------------- #
# 合成密钥工具（仅用于本地夹具，绝不是生产账户）
# --------------------------------------------------------------------------- #
def generate_private_key() -> bytes:
    """生成一个安全随机 secp256k1 私钥（32 字节）。"""
    return keys.PrivateKey(secrets.token_bytes(32)).to_bytes()


def private_key_from_hex(value: str) -> bytes:
    try:
        return keys.PrivateKey(decode_hex(value)).to_bytes()
    except (ValueError, TypeError) as exc:
        raise TxError(
            ErrorCode.MALFORMED_TRANSACTION,
            f"私钥格式非法: {exc}",
        ) from exc


def address_for_private_key(private_key: bytes) -> str:
    return keys.PrivateKey(private_key).public_key.to_checksum_address()


def normalize_address(value: str) -> str:
    """返回 EIP-55 校验地址；非法时抛 MALFORMED_TRANSACTION。"""
    if not isinstance(value, str) or not is_address(value):
        raise TxError(
            ErrorCode.MALFORMED_TRANSACTION,
            f"非法地址: {value!r}",
        )
    return to_checksum_address(value)


def normalize_tx_hash(value: str) -> str:
    if (
        not isinstance(value, str)
        or not is_0x_prefixed(value)
        or len(value) != 66
    ):
        raise TxError(
            ErrorCode.MALFORMED_TRANSACTION,
            f"非法交易哈希: {value!r}",
        )
    try:
        decode_hex(value)
    except (ValueError, TypeError) as exc:
        raise TxError(
            ErrorCode.MALFORMED_TRANSACTION,
            f"非法交易哈希: {value!r}",
        ) from exc
    return value.lower()


# --------------------------------------------------------------------------- #
# RLP 编解码
# --------------------------------------------------------------------------- #
# 字段顺序固定，任何回放/重验都按同一布局解码。
_UNSIGNED_FIELDS = ("nonce", "gas_price", "gas_limit", "to", "value", "data")


def _to_bytes(value: str | bytes | None) -> bytes:
    if value is None or value == "":
        return b""
    if isinstance(value, bytes):
        return value
    if is_0x_prefixed(value):
        return decode_hex(value)
    raise TxError(
        ErrorCode.MALFORMED_TRANSACTION,
        f"字节字段必须为 0x 前缀十六进制: {value!r}",
    )


def _unsigned_list(
    *,
    nonce: int,
    gas_price: int,
    gas_limit: int,
    to: str,
    value: int,
    data: bytes,
) -> list[object]:
    if not isinstance(nonce, int) or nonce < 0:
        raise TxError(ErrorCode.MALFORMED_TRANSACTION, "nonce 必须为非负整数")
    if not isinstance(gas_price, int) or gas_price < 0:
        raise TxError(ErrorCode.MALFORMED_TRANSACTION, "gas_price 必须为非负整数")
    if not isinstance(gas_limit, int) or gas_limit <= 0:
        raise TxError(ErrorCode.MALFORMED_TRANSACTION, "gas_limit 必须为正整数")
    if not isinstance(value, int) or value < 0:
        raise TxError(ErrorCode.MALFORMED_TRANSACTION, "value 必须为非负整数")
    if not isinstance(data, (bytes, bytearray)):
        raise TxError(ErrorCode.MALFORMED_TRANSACTION, "data 必须为字节串")
    to_addr = normalize_address(to)
    return [
        nonce,
        gas_price,
        gas_limit,
        decode_hex(to_addr),
        value,
        bytes(data),
    ]


def signing_hash(
    *,
    nonce: int,
    gas_price: int,
    gas_limit: int,
    to: str,
    value: int,
    data: bytes,
    chain_id: int,
) -> bytes:
    """EIP-155 待签名摘要（32 字节）。"""
    unsigned = _unsigned_list(
        nonce=nonce,
        gas_price=gas_price,
        gas_limit=gas_limit,
        to=to,
        value=value,
        data=data,
    )
    return keccak(rlp.encode(unsigned + [chain_id, 0, 0]))


def sign_transaction(
    *,
    private_key: bytes,
    nonce: int,
    gas_price: int,
    gas_limit: int,
    to: str,
    value: int,
    data: str | bytes = b"",
    chain_id: int = LOCAL_CHAIN_ID,
) -> Transaction:
    """构造并签名一笔交易，返回已验签的 ``Transaction`` 值对象。"""

    payload = _to_bytes(data)
    msg_hash = signing_hash(
        nonce=nonce,
        gas_price=gas_price,
        gas_limit=gas_limit,
        to=to,
        value=value,
        data=payload,
        chain_id=chain_id,
    )
    signer = keys.PrivateKey(private_key)
    signature = signer.sign_msg_hash(msg_hash)
    # EIP-155：v = recovery_id + chain_id*2 + 35
    v = signature.v + chain_id * 2 + 35
    r, s = signature.r, signature.s
    unsigned = _unsigned_list(
        nonce=nonce,
        gas_price=gas_price,
        gas_limit=gas_limit,
        to=to,
        value=value,
        data=payload,
    )
    raw = rlp.encode(unsigned + [v, r, s])
    tx_hash = keccak(raw)
    sender = signer.public_key.to_checksum_address()
    return Transaction(
        tx_hash=encode_hex(tx_hash),
        sender=sender,
        nonce=nonce,
        gas_price=gas_price,
        gas_limit=gas_limit,
        to=to_checksum_address(to) if to else to,
        value=value,
        data=payload,
        v=v,
        r=r,
        s=s,
        raw=raw,
    )


def decode_signed_transaction(
    raw: bytes,
    *,
    expected_chain_id: int = LOCAL_CHAIN_ID,
) -> Transaction:
    """从 RLP 原始字节解码并**完整验签**。

    校验内容：RLP 结构、整数范围、链 id、低 s、v 合法、恢复出的公钥一致。
    任何一步失败都给出具体 ``ErrorCode``，不接受"看起来像交易"的输入。
    """

    if not isinstance(raw, (bytes, bytearray)):
        raise TxError(
            ErrorCode.MALFORMED_TRANSACTION, "raw 交易必须为字节串"
        )
    try:
        decoded = rlp.decode(bytes(raw))
    except (rlp.exceptions.DecodingError, Exception) as exc:  # noqa: BLE001
        raise TxError(
            ErrorCode.MALFORMED_TRANSACTION,
            f"RLP 解码失败: {exc}",
        ) from exc

    if len(decoded) != 9:
        raise TxError(
            ErrorCode.MALFORMED_TRANSACTION,
            f"签名交易必须含 9 个字段，实际 {len(decoded)}",
        )

    def _int(field: bytes, name: str) -> int:
        try:
            return int.from_bytes(field, byteorder="big", signed=False)
        except Exception as exc:  # noqa: BLE001
            raise TxError(
                ErrorCode.MALFORMED_TRANSACTION, f"{name} 不是大端整数"
            ) from exc

    nonce_b, gp_b, gl_b, to_b, value_b, data_b, v_b, r_b, s_b = decoded
    nonce = _int(nonce_b, "nonce")
    gas_price = _int(gp_b, "gas_price")
    gas_limit = _int(gl_b, "gas_limit")
    value = _int(value_b, "value")
    v = _int(v_b, "v")
    r = _int(r_b, "r")
    s = _int(s_b, "s")
    data = bytes(data_b)
    to_bytes = bytes(to_b)

    if len(to_bytes) not in (0, 20):
        raise TxError(
            ErrorCode.MALFORMED_TRANSACTION,
            "to 必须为空（合约创建）或 20 字节",
        )
    to_addr = (
        to_checksum_address(encode_hex(to_bytes)) if to_bytes else ""
    )

    if gas_limit <= 0:
        raise TxError(
            ErrorCode.MALFORMED_TRANSACTION, "gas_limit 必须为正整数"
        )
    if nonce < 0 or gas_price < 0 or value < 0:
        raise TxError(
            ErrorCode.MALFORMED_TRANSACTION, "金额/nonce/gas_price 非法"
        )

    # EIP-155 链 id 恢复
    base = expected_chain_id * 2 + 35
    if v not in (base, base + 1):
        raise TxError(
            ErrorCode.WRONG_CHAIN_ID,
            f"v={v} 与 chain_id={expected_chain_id} 不匹配"
            f"（期望 {base} 或 {base + 1}）",
            details={"v": v, "chain_id": expected_chain_id},
        )
    recovery_id = v - base

    if not (1 <= r < SECPK1_N) or not (1 <= s < SECPK1_N):
        raise TxError(
            ErrorCode.INVALID_SIGNATURE, "r/s 超出 secp256k1 阶范围"
        )
    # EIP-2 低 s 唯一性
    if s > SECPK1_N // 2:
        raise TxError(
            ErrorCode.INVALID_SIGNATURE,
            "签名 s 为高值，拒绝可塑性签名（EIP-2 low-s）",
        )

    try:
        unsigned = _unsigned_list(
            nonce=nonce,
            gas_price=gas_price,
            gas_limit=gas_limit,
            to=to_addr,
            value=value,
            data=data,
        )
    except TxError:
        raise
    msg_hash = keccak(rlp.encode(unsigned + [expected_chain_id, 0, 0]))

    try:
        signature = keys.Signature(vrs=(recovery_id, r, s))
        recovered = signature.recover_public_key_from_msg_hash(msg_hash)
    except (BadSignature, Exception) as exc:  # noqa: BLE001
        raise TxError(
            ErrorCode.INVALID_SIGNATURE,
            f"公钥恢复失败: {exc}",
        ) from exc

    tx_hash = encode_hex(keccak(bytes(raw)))
    sender = recovered.to_checksum_address()
    return Transaction(
        tx_hash=tx_hash,
        sender=sender,
        nonce=nonce,
        gas_price=gas_price,
        gas_limit=gas_limit,
        to=to_addr,
        value=value,
        data=data,
        v=v,
        r=r,
        s=s,
        raw=bytes(raw),
    )


def intrinsic_gas(
    tx: Transaction, *, base_gas: int, per_data_byte: int
) -> int:
    """简化的内在 gas：基础 21000 + 每负载字节 16。"""
    return base_gas + per_data_byte * len(tx.data)


def hex_address_normalized(value: str) -> str:
    """供外部（API/夹具）统一规范化地址。"""
    return to_normalized_address(value)
