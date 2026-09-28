"""确定性编码、哈希与签名验签。

* ``encode`` / ``decode``：自有规范二进制编码（带类型标签、字典键排序、
  整数最短表示），保证跨进程字节一致；
* ``canonical_hash``：对编码结果取 SHA-256，作为输入摘要与状态根；
* ``KeyPair`` / ``sign_transaction`` / ``verify_transaction``：基于成熟库
  ``cryptography`` 的 Ed25519。教学场景使用合成密钥（可由 32 字节种子
  确定性派生），无任何真实账号。

宿主的时间与随机源不出现在本模块的任何输出中。
"""
from __future__ import annotations

import hashlib
from typing import Any, Iterable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

# ---------------------------------------------------------------------------
# 类型标签
# ---------------------------------------------------------------------------
_TAG_BYTES = 0x00
_TAG_STR = 0x01
_TAG_INT = 0x02
_TAG_FALSE = 0x03
_TAG_TRUE = 0x04
_TAG_NULL = 0x05
_TAG_LIST = 0x06
_TAG_DICT = 0x07

_ADDRESS_LEN = 20  # 地址 = sha256(pubkey) 的前 20 字节


class EncodingError(ValueError):
    """值无法被规范编码（类型不受支持 / 整数越界等）。"""


def _encode_length(length: int) -> bytes:
    if length < 0:
        raise EncodingError("负长度")
    parts = [length & 0x7F]
    length >>= 7
    while length:
        parts.append((length & 0x7F) | 0x80)
        length >>= 7
    return bytes(reversed(parts))


def _read_length(data: bytes, pos: int) -> tuple[int, int]:
    shift = 0
    value = 0
    while True:
        if pos >= len(data):
            raise EncodingError("长度字段被截断")
        b = data[pos]
        pos += 1
        value = (value << 7) | (b & 0x7F)
        if not (b & 0x80):
            break
        shift += 7
        if shift > 63:
            raise EncodingError("长度字段过长")
    return value, pos


def encode(value: Any) -> bytes:
    """将 Python 值编码为确定字节序列。

    支持：bytes、str、int（受 64 位范围约束，与虚拟机一致）、bool、None、
    list/tuple、dict（键必须为 str，输出按键的 UTF-8 字节序排序）。
    """
    if isinstance(value, bool):
        return bytes([_TAG_TRUE if value else _TAG_FALSE])
    if value is None:
        return bytes([_TAG_NULL])
    if isinstance(value, int):
        if not (-(1 << 63) <= value < (1 << 63)):
            raise EncodingError("整数超出 64 位有符号范围")
        # 符号字节 + 最短大端绝对值
        neg = value < 0
        mag = abs(value)
        body = mag.to_bytes(max(1, (mag.bit_length() + 7) // 8), "big", signed=False)
        payload = bytes([1 if neg else 0]) + body
        return bytes([_TAG_INT]) + _encode_length(len(payload)) + payload
    if isinstance(value, str):
        body = value.encode("utf-8")
        return bytes([_TAG_STR]) + _encode_length(len(body)) + body
    if isinstance(value, bytes):
        return bytes([_TAG_BYTES]) + _encode_length(len(value)) + value
    if isinstance(value, (list, tuple)):
        body = b"".join(encode(v) for v in value)
        return bytes([_TAG_LIST]) + _encode_length(len(body)) + body
    if isinstance(value, dict):
        chunks: list[bytes] = []
        for key in sorted(value.keys(), key=lambda k: k.encode("utf-8")):
            if not isinstance(key, str):
                raise EncodingError("字典键必须为字符串")
            chunks.append(encode(key))
            chunks.append(encode(value[key]))
        body = b"".join(chunks)
        return bytes([_TAG_DICT]) + _encode_length(len(body)) + body
    raise EncodingError(f"不支持编码的类型: {type(value).__name__}")


def decode(data: bytes) -> Any:
    value, pos = _decode_at(data, 0)
    if pos != len(data):
        raise EncodingError("编码后存在多余字节")
    return value


def _take(data: bytes, pos: int, n: int) -> bytes:
    if pos + n > len(data):
        raise EncodingError("载荷被截断")
    return data[pos : pos + n]


def _decode_at(data: bytes, pos: int) -> tuple[Any, int]:
    if pos >= len(data):
        raise EncodingError("缺少类型标签")
    tag = data[pos]
    pos += 1
    if tag in (_TAG_TRUE, _TAG_FALSE, _TAG_NULL):
        return (tag == _TAG_TRUE) if tag != _TAG_NULL else None, pos
    length, pos = _read_length(data, pos)
    payload = _take(data, pos, length)
    pos += length
    if tag == _TAG_STR:
        return payload.decode("utf-8"), pos
    if tag == _TAG_BYTES:
        return payload, pos
    if tag == _TAG_INT:
        if len(payload) < 1:
            raise EncodingError("整数载荷为空")
        neg = payload[0] == 1
        mag = int.from_bytes(payload[1:], "big", signed=False)
        return (-mag if neg else mag), pos
    if tag == _TAG_LIST:
        items: list[Any] = []
        sub = 0
        while sub < len(payload):
            item, sub = _decode_at(payload, sub)
            items.append(item)
        return items, pos
    if tag == _TAG_DICT:
        result: dict[str, Any] = {}
        sub = 0
        while sub < len(payload):
            k, sub = _decode_at(payload, sub)
            v, sub = _decode_at(payload, sub)
            if not isinstance(k, str):
                raise EncodingError("字典键不是字符串")
            result[k] = v
        return result, pos
    raise EncodingError(f"未知类型标签: 0x{tag:02x}")


def canonical_hash(value: Any) -> bytes:
    """规范哈希（SHA-256）。"""
    return hashlib.sha256(encode(value)).digest()


def hexhash(value: Any) -> str:
    return canonical_hash(value).hex()


# ---------------------------------------------------------------------------
# 密钥与地址（合成教学密钥）
# ---------------------------------------------------------------------------
class KeyPair:
    """Ed25519 密钥对。可由 32 字节种子确定性重建（夹具用）。"""

    def __init__(self, private_key: Ed25519PrivateKey):
        self._sk = private_key
        self._pk: Ed25519PublicKey = private_key.public_key()

    @classmethod
    def from_seed(cls, seed: bytes) -> "KeyPair":
        if len(seed) != 32:
            raise ValueError("种子必须为 32 字节")
        return cls(Ed25519PrivateKey.from_private_bytes(seed))

    @classmethod
    def generate(cls) -> "KeyPair":
        # 仅用于本地生成合成密钥；执行路径从不调用这里（无随机性进入共识）
        return cls(Ed25519PrivateKey.generate())

    def public_bytes(self) -> bytes:
        return self._pk.public_bytes_raw()

    def private_bytes(self) -> bytes:
        return self._sk.private_bytes_raw()

    def address(self) -> bytes:
        return hashlib.sha256(self.public_bytes()).digest()[:_ADDRESS_LEN]

    def address_hex(self) -> str:
        return self.address().hex()

    def sign(self, message: bytes) -> bytes:
        return self._sk.sign(message)


def address_from_public(public_key: bytes) -> str:
    if len(public_key) != 32:
        raise ValueError("Ed25519 公钥应为 32 字节")
    return hashlib.sha256(public_key).digest()[:_ADDRESS_LEN].hex()


# ---------------------------------------------------------------------------
# 交易签名与验签
# ---------------------------------------------------------------------------
# 签名时从交易对象中剔除的字段：签名本身、内嵌签名者、公钥
# （被签内容固定为 chain/nonce/code/gas_limit，便于各端实现）
_UNSIGNED_OMIT = frozenset({"signature", "signer", "pubkey"})


def transaction_signing_payload(transaction: dict[str, Any]) -> bytes:
    """构造待签名摘要：剔除签名字段后做规范编码。"""
    unsigned = {k: v for k, v in transaction.items() if k not in _UNSIGNED_OMIT}
    return encode(unsigned)


def transaction_digest(transaction: dict[str, Any]) -> str:
    """交易内容（不含签名）的规范哈希十六进制。"""
    return hashlib.sha256(transaction_signing_payload(transaction)).digest().hex()


def sign_transaction(keypair: KeyPair, transaction: dict[str, Any]) -> dict[str, Any]:
    """返回带 signature 与 signer 的新交易字典（不改原对象）。"""
    signed = {k: v for k, v in transaction.items() if k not in _UNSIGNED_OMIT}
    sig = keypair.sign(transaction_signing_payload(transaction))
    signed["signature"] = sig.hex()
    signed["signer"] = keypair.address_hex()
    return signed


def verify_transaction(transaction: dict[str, Any]) -> str:
    """校验签名，返回签名者地址 hex；失败抛 ``SignatureError``。

    校验同时保证交易内嵌的 signer 与公钥推出的地址一致。
    """
    sig_hex = transaction.get("signature")
    signer = transaction.get("signer")
    pubkey_hex = transaction.get("pubkey")
    if not isinstance(sig_hex, str) or not isinstance(signer, str) or not isinstance(pubkey_hex, str):
        raise SignatureError("缺少 signature / signer / pubkey 字段")
    try:
        sig = bytes.fromhex(sig_hex)
        pubkey_bytes = bytes.fromhex(pubkey_hex)
    except ValueError as exc:
        raise SignatureError("签名或公钥不是合法十六进制") from exc
    try:
        derived = address_from_public(pubkey_bytes)
    except ValueError as exc:
        raise SignatureError(str(exc)) from exc
    if derived != signer.lower():
        raise SignatureError("signer 与公钥推导地址不一致")
    try:
        Ed25519PublicKey.from_public_bytes(pubkey_bytes).verify(
            sig, transaction_signing_payload(transaction)
        )
    except InvalidSignature as exc:
        raise SignatureError("签名验证失败") from exc
    return derived


class SignatureError(ValueError):
    """交易签名无效。"""


def iter_fixture_keypairs() -> Iterable[KeyPair]:
    """确定性合成密钥对集合（测试/示例用，绝非真实密钥）。"""
    for i in range(8):
        yield KeyPair.from_seed(hashlib.sha256(f"teaching-chain-fixture-{i}".encode()).digest())
