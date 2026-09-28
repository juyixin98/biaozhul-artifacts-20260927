"""编码与验签：严格 RLP 编解码 + EIP-155 遗留交易签名/恢复。

设计要点
--------
* RLP 编解码在本文件内**手写实现**，不依赖第三方 ``rlp`` 包；第三方
  ``rlp`` 只在测试套件中作为独立预言机，与本实现互编互解交叉核验，
  防止“参考实现和被测实现是同一份代码”。
* 解码是**严格**的：拒绝非规范整数（前导零）、非规范长度前缀、尾部多余字节，
  从而消除同一条交易的多编码歧义。
* 签名为 EIP-155 遗留交易：签名载荷 ``[nonce,gas_price,gas,to,value,data,chain_id,0,0]``，
  ``v = chain_id*2 + 35 + recovery_id``。拒绝非 EIP-155、错误链 ID、
  越界 ``r/s`` 与高 ``s``（交易延展性）。
* secp256k1 原语来自成熟库 ``eth-keys``；keccak-256 来自 ``eth-hash``
  （pycryptodome 后端）。
"""

from __future__ import annotations

from dataclasses import dataclass

from eth_hash.auto import keccak
from eth_keys import keys as ek_keys

from .errors import (
    InvalidSignature,
    IntrinsicGasTooLow,
    TxDecodeError,
    WrongChainId,
)

# secp256k1 曲线阶（用于 r/s 合法性与低 s 检查）
SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
SECP256K1_HALF_N = SECP256K1_N // 2

# 遗留转账固有 gas（EIP-2028 数据定价：零字节 4，非零字节 16）
INTRINSIC_GAS_TRANSFER = 21_000
GZERO = 4
GNONZERO = 16
MIN_GAS_LIMIT = 21_000


# --------------------------------------------------------------------------- #
# 十六进制工具
# --------------------------------------------------------------------------- #
def hex_to_bytes(value: str, *, name: str = "value") -> bytes:
    """把 ``0x`` 前缀的严格十六进制字符串转为 bytes。"""
    if not isinstance(value, str) or not value.startswith("0x"):
        raise TxDecodeError(f"{name} must be a 0x-prefixed hex string")
    body = value[2:]
    if len(body) % 2:
        raise TxDecodeError(f"{name} has odd-length hex body")
    try:
        return bytes.fromhex(body)
    except ValueError as exc:
        raise TxDecodeError(f"{name} is not valid hex: {exc}") from exc


def to_hex0x(data: bytes) -> str:
    return "0x" + data.hex()


def to_checksum_address(raw: bytes | str) -> str:
    """EIP-55 混合大小写校验地址（手写，避免地址比较时大小写歧义）。

    接受 20 字节或 ``0x`` 前缀的 40 位十六进制字符串。
    """
    if isinstance(raw, str):
        if not raw.startswith("0x") or len(raw) != 42:
            raise ValueError("address string must be 0x + 40 hex chars")
        raw = bytes.fromhex(raw[2:])
    if len(raw) != 20:
        raise ValueError("address must be 20 bytes")
    low = raw.hex()
    h = keccak(low.encode("ascii")).hex()
    out = "".join(c.upper() if c.isalpha() and int(h[i], 16) >= 8 else c for i, c in enumerate(low))
    return "0x" + out


def canonical_address(value: str) -> bytes:
    raw = hex_to_bytes(value, name="address")
    if len(raw) != 20:
        raise TxDecodeError("address must be 20 bytes")
    return raw


# --------------------------------------------------------------------------- #
# 手写 RLP（严格规范编码）
# --------------------------------------------------------------------------- #
def _int_to_min_bytes(value: int) -> bytes:
    if value < 0:
        raise ValueError("rlp cannot encode negative integers")
    return value.to_bytes((value.bit_length() + 7) // 8, "big") if value else b""


def rlp_encode(item: bytes | list) -> bytes:
    """编码 bytes 或嵌套 list 为 RLP。"""
    if isinstance(item, bytes):
        n = len(item)
        if n == 1 and item[0] < 0x80:
            return item
        if n <= 55:
            return bytes([0x80 + n]) + item
        length = _int_to_min_bytes(n)
        return bytes([0xB7 + len(length)]) + length + item
    if isinstance(item, list):
        payload = b"".join(rlp_encode(x) for x in item)
        n = len(payload)
        if n <= 55:
            return bytes([0xC0 + n]) + payload
        length = _int_to_min_bytes(n)
        return bytes([0xF7 + len(length)]) + length + payload
    raise TypeError(f"cannot rlp-encode {type(item)!r}")


def _decode_one(buf: bytes, start: int) -> tuple[bytes | list, int]:
    """返回 (解码项, 下一偏移)。逐项检查规范编码。"""
    if start >= len(buf):
        raise TxDecodeError("rlp: unexpected end of input")
    prefix = buf[start]

    # 单字节
    if prefix < 0x80:
        return bytes([prefix]), start + 1

    # 短字符串
    if prefix < 0xB8:
        n = prefix - 0x80
        end = start + 1 + n
        if end > len(buf):
            raise TxDecodeError("rlp: short string overruns input")
        data = buf[start + 1 : end]
        # 规范检查：长度 1 且字节 < 0x80 必须使用单字节形式
        if n == 1 and data[0] < 0x80:
            raise TxDecodeError("rlp: non-canonical encoding of single byte")
        return data, end

    # 长字符串
    if prefix < 0xC0:
        llen = prefix - 0xB7
        lp_start = start + 1
        lp_end = lp_start + llen
        if lp_end > len(buf):
            raise TxDecodeError("rlp: long-string length overruns input")
        length_bytes = buf[lp_start:lp_end]
        # 规范检查：长度字段必须至少 1 字节且无前置零（长度为 0 用 0x80 表示）
        if llen == 0 or length_bytes[0] == 0:
            raise TxDecodeError("rlp: non-canonical long-string length (zero or leading zero)")
        # 规范检查：实际长度 <= 55 时不应使用长形式
        n = int.from_bytes(length_bytes, "big")
        if n <= 55:
            raise TxDecodeError("rlp: long string used for short payload")
        end = lp_end + n
        if end > len(buf):
            raise TxDecodeError("rlp: long-string payload overruns input")
        return buf[lp_end:end], end

    # 短列表
    if prefix < 0xF8:
        n = prefix - 0xC0
        end = start + 1 + n
        if end > len(buf):
            raise TxDecodeError("rlp: short list overruns input")
        return _decode_list_payload(buf, start + 1, end), end

    # 长列表
    llen = prefix - 0xF7
    lp_start = start + 1
    lp_end = lp_start + llen
    if lp_end > len(buf):
        raise TxDecodeError("rlp: long-list length overruns input")
    length_bytes = buf[lp_start:lp_end]
    if length_bytes[0] == 0:
        raise TxDecodeError("rlp: non-canonical long-list length (leading zero)")
    n = int.from_bytes(length_bytes, "big")
    if n <= 55:
        raise TxDecodeError("rlp: long list used for short payload")
    end = lp_end + n
    if end > len(buf):
        raise TxDecodeError("rlp: long-list payload overruns input")
    return _decode_list_payload(buf, lp_end, end), end


def _decode_list_payload(buf: bytes, start: int, end: int) -> list:
    items: list = []
    pos = start
    while pos < end:
        item, pos = _decode_one(buf, pos)
        items.append(item)
    if pos != end:
        raise TxDecodeError("rlp: list element overruns declared length")
    return items


def rlp_decode_exact(buf: bytes) -> bytes | list:
    """严格解码且要求整段字节恰好消耗完。"""
    item, end = _decode_one(buf, 0)
    if end != len(buf):
        raise TxDecodeError("rlp: trailing bytes after top-level item")
    return item


def _decode_uint(raw: bytes, *, name: str, max_bits: int | None = None) -> int:
    # 规范整数：0 必须编码为空串；任何前导零（含单个 0x00）都非法
    if len(raw) >= 1 and raw[0] == 0:
        raise TxDecodeError(f"{name}: non-canonical integer (leading zero)")
    value = int.from_bytes(raw, "big")
    if max_bits is not None and value.bit_length() > max_bits:
        raise TxDecodeError(f"{name}: exceeds {max_bits} bits")
    return value


# --------------------------------------------------------------------------- #
# 遗留交易
# --------------------------------------------------------------------------- #
def signing_payload(
    nonce: int,
    gas_price: int,
    gas_limit: int,
    to: bytes,
    value: int,
    data: bytes,
    chain_id: int,
) -> list:
    return [
        _int_to_min_bytes(nonce),
        _int_to_min_bytes(gas_price),
        _int_to_min_bytes(gas_limit),
        to,
        _int_to_min_bytes(value),
        data,
        _int_to_min_bytes(chain_id),
        b"",
        b"",
    ]


def intrinsic_gas(data: bytes) -> int:
    return INTRINSIC_GAS_TRANSFER + sum(GZERO if b == 0 else GNONZERO for b in data)


@dataclass(frozen=True, slots=True)
class SignedTransaction:
    """已签名的 EIP-155 遗留交易。"""

    nonce: int
    gas_price: int
    gas_limit: int
    to: bytes  # 20 字节；合约创建为空字节（本项目夹具不使用创建）
    value: int
    data: bytes
    v: int
    r: int
    s: int
    chain_id: int
    from_address: bytes

    # ---- 序列化 ---- #
    def to_rlp(self) -> bytes:
        return rlp_encode(
            [
                _int_to_min_bytes(self.nonce),
                _int_to_min_bytes(self.gas_price),
                _int_to_min_bytes(self.gas_limit),
                self.to,
                _int_to_min_bytes(self.value),
                self.data,
                _int_to_min_bytes(self.v),
                _int_to_min_bytes(self.r),
                _int_to_min_bytes(self.s),
            ]
        )

    def hash(self) -> bytes:
        return keccak(self.to_rlp())

    # ---- 展示 ---- #
    @property
    def sender(self) -> str:
        return to_checksum_address(self.from_address)

    @property
    def to_address(self) -> str | None:
        return to_checksum_address(self.to) if self.to else None

    @property
    def max_cost(self) -> int:
        """执行所需的最大余额：value + gas_limit*gas_price。"""
        return self.value + self.gas_limit * self.gas_price

    @property
    def fee_cap(self) -> int:
        return self.gas_limit * self.gas_price

    @property
    def intrinsic_gas_required(self) -> int:
        return intrinsic_gas(self.data)

    def to_public_dict(self) -> dict:
        return {
            "hash": to_hex0x(self.hash()),
            "from": self.sender,
            "to": self.to_address,
            "nonce": self.nonce,
            "gas_price": str(self.gas_price),
            "gas_limit": self.gas_limit,
            "value": str(self.value),
            "data": to_hex0x(self.data),
            "chain_id": self.chain_id,
            "v": self.v,
            "r": hex(self.r),
            "s": hex(self.s),
            "max_cost": str(self.max_cost),
            "intrinsic_gas": self.intrinsic_gas_required,
        }


def _parse_field_list(items: list) -> tuple[int, int, int, bytes, int, bytes, int, int, int]:
    if len(items) != 9:
        raise TxDecodeError(f"signed legacy tx must have 9 fields, got {len(items)}")
    if not all(isinstance(x, bytes) for x in items):
        raise TxDecodeError("tx fields must all be byte strings")
    nonce_b, gp_b, gl_b, to_b, value_b, data_b, v_b, r_b, s_b = items

    nonce = _decode_uint(nonce_b, name="nonce", max_bits=64)
    gas_price = _decode_uint(gp_b, name="gas_price", max_bits=256)
    gas_limit = _decode_uint(gl_b, name="gas_limit", max_bits=64)
    value = _decode_uint(value_b, name="value", max_bits=256)
    if len(to_b) not in (0, 20):
        raise TxDecodeError("to must be empty or 20 bytes")
    v = _decode_uint(v_b, name="v", max_bits=64)
    r = _decode_uint(r_b, name="r", max_bits=256)
    s = _decode_uint(s_b, name="s", max_bits=256)
    return nonce, gas_price, gas_limit, to_b, value, bytes(data_b), v, r, s


def decode_signed(raw: bytes, *, expected_chain_id: int) -> SignedTransaction:
    """严格解码并验签一条已签名交易。

    抛 :class:`TxDecodeError` / :class:`InvalidSignature` /
    :class:`WrongChainId` / :class:`IntrinsicGasTooLow`。
    """
    top = rlp_decode_exact(raw)
    if not isinstance(top, list):
        raise TxDecodeError("signed tx must be an RLP list")
    nonce, gas_price, gas_limit, to, value, data, v, r, s = _parse_field_list(top)

    # EIP-155：v 必须形如 2*chain_id + 35 + {0,1}
    if v < 35 or ((v - 35) % 2) not in (0, 1):
        raise InvalidSignature(
            "v is not an EIP-155 signature marker",
            details={"v": v, "expected_form": "2*chain_id+35 or 2*chain_id+36"},
        )
    chain_id = (v - 35) // 2
    if chain_id != expected_chain_id:
        raise WrongChainId(
            f"chain id mismatch: tx={chain_id} node={expected_chain_id}",
            details={"tx_chain_id": chain_id, "node_chain_id": expected_chain_id},
        )
    recovery_id = (v - 35) & 1

    # r/s 边界与低 s（防延展性）
    if not (1 <= r < SECP256K1_N):
        raise InvalidSignature("r out of range", details={"r": hex(r)})
    if not (1 <= s < SECP256K1_N):
        raise InvalidSignature("s out of range", details={"s": hex(s)})
    if s > SECP256K1_HALF_N:
        raise InvalidSignature("s is above half-order (high-s malleable signature)")

    payload = rlp_encode(
        signing_payload(nonce, gas_price, gas_limit, to, value, data, chain_id)
    )
    msg_hash = keccak(payload)
    try:
        signature = ek_keys.Signature(vrs=(recovery_id, r, s))
        public_key = signature.recover_public_key_from_msg_hash(msg_hash)
    except Exception as exc:  # eth_keys 对不可恢复点抛 ValueError
        raise InvalidSignature(f"signature recovery failed: {exc}") from exc

    if gas_limit < MIN_GAS_LIMIT:
        raise IntrinsicGasTooLow(
            f"gas_limit {gas_limit} below minimum {MIN_GAS_LIMIT}",
            details={"gas_limit": gas_limit, "minimum": MIN_GAS_LIMIT},
        )
    required = intrinsic_gas(data)
    if gas_limit < required:
        raise IntrinsicGasTooLow(
            f"gas_limit {gas_limit} below intrinsic gas {required}",
            details={"gas_limit": gas_limit, "intrinsic_gas": required},
        )

    return SignedTransaction(
        nonce=nonce,
        gas_price=gas_price,
        gas_limit=gas_limit,
        to=to,
        value=value,
        data=data,
        v=v,
        r=r,
        s=s,
        chain_id=chain_id,
        from_address=public_key.to_canonical_address(),
    )


def sign_transaction(
    private_key: bytes,
    *,
    nonce: int,
    gas_price: int,
    gas_limit: int,
    to: bytes,
    value: int,
    data: bytes = b"",
    chain_id: int,
) -> SignedTransaction:
    """本地合成夹具用的签名助手；正常节点只应接收已签名的原始交易。"""
    if len(private_key) != 32:
        raise ValueError("private key must be 32 bytes")
    signer = ek_keys.PrivateKey(private_key)
    payload = rlp_encode(
        signing_payload(nonce, gas_price, gas_limit, to, value, data, chain_id)
    )
    msg_hash = keccak(payload)
    signature = signer.sign_msg_hash(msg_hash)  # eth_keys 产出低 s 签名
    recid, r, s = signature.vrs
    if gas_limit < intrinsic_gas(data):
        raise IntrinsicGasTooLow(
            "gas_limit below intrinsic gas",
            details={"gas_limit": gas_limit, "intrinsic_gas": intrinsic_gas(data)},
        )
    return SignedTransaction(
        nonce=nonce,
        gas_price=gas_price,
        gas_limit=gas_limit,
        to=to,
        value=value,
        data=data,
        v=chain_id * 2 + 35 + recid,
        r=r,
        s=s,
        chain_id=chain_id,
        from_address=signer.public_key.to_canonical_address(),
    )
