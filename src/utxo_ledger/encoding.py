"""编码与固定摘要模块 (canonical encoding & hashing boundary).

本模块是跨实现契约：离线参考实现 ``reference/oracle.py`` 必须能独立复现这里的
字节布局与摘要规则（该模块不 import 本包），因此布局刻意简单、全部大端、定长。

字节级约定
==========
* 整数：u32 / u64 均为大端；哈希与公钥使用定长字节。
* 可变长度：Bitcoin 风格 varint（<0xfd 单字节；0xfd + u16；0xfe + u32；0xff + u64）。
* 交易体（无见证）魔数 ``UTXT``，其 sha256 即 txid；签名摘要为
  ``sha256(b"utxo-ledger/sighash/v1" + body)`` —— 见证不参与签名摘要。
* 含见证完整交易魔数 ``UTXW``。
* 块头魔数 ``UHDR``，定长 116 字节；block_id = sha256(encode_header)。
* 完整块魔数 ``UBLK`` + 块头载荷 + u32 交易数 + 各完整交易。
* 所有根均为迭代哈希链：h0=sha256(prefix)，h_{i+1}=sha256(h_i + item)。

JSON 为严格模式：键集合必须精确匹配、整数不得为 bool、字节字段必须为偶数位 hex。
JSON 解码只做"能否按固定布局结构化"的检查；金额范围/零值等语义检查由内核负责。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable

from .errors import MalformedEncodingError

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
PROTOCOL_VERSION = 1
HASH_SIZE = 32
PUBKEY_COMPRESSED_SIZE = 33

_MAX_U32 = 0xFFFFFFFF
_MAX_U64 = 0xFFFFFFFFFFFFFFFF
_MIN_I64 = -(1 << 63)
_MAX_I64 = (1 << 63) - 1  # MAX_MONEY（内核再引用一次做语义约束）

_SIGHASH_TAG = b"utxo-ledger/sighash/v1"
_TXROOT_TAG = b"utxo-ledger/txroot/v1"
_WITROOT_TAG = b"utxo-ledger/witroot/v1"
_UTXOROOT_TAG = b"utxo-ledger/utxoroot/v1"

ZERO_HASH = b"\x00" * HASH_SIZE


# ---------------------------------------------------------------------------
# 领域对象（不可值对象，跨模块数据契约）
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Outpoint:
    """对某笔交易第 vout 个输出的引用。"""

    txid: bytes  # 恰好 32 字节
    vout: int

    def to_dict(self) -> dict[str, Any]:
        return {"txid": self.txid.hex(), "vout": self.vout}


@dataclass(frozen=True, slots=True)
class TxInput:
    prev: Outpoint


@dataclass(frozen=True, slots=True)
class TxOutput:
    amount: int
    pubkey: bytes  # 恰好 33 字节压缩公钥（资产接收方）


@dataclass(frozen=True, slots=True)
class Witness:
    """与输入一一对应的见证；signature 为 ECDSA(secp256k1) DER 签名。"""

    signature: bytes


@dataclass(frozen=True, slots=True)
class Transaction:
    version: int
    inputs: tuple[TxInput, ...]
    outputs: tuple[TxOutput, ...]
    fee: int
    witnesses: tuple[Witness, ...]

    @property
    def is_issue(self) -> bool:
        """无输入交易 = 发行（仅 genesis 块允许）。"""
        return not self.inputs


@dataclass(frozen=True, slots=True)
class BlockHeader:
    version: int
    height: int
    prev_hash: bytes  # 32 字节；genesis 为 ZERO_HASH
    timestamp: int  # u64 秒；仅编码固定，不做时钟共识检查
    tx_root: bytes  # 32
    witness_root: bytes  # 32


@dataclass(frozen=True, slots=True)
class Block:
    header: BlockHeader
    transactions: tuple[Transaction, ...]


# ---------------------------------------------------------------------------
# 低层原语
# ---------------------------------------------------------------------------
def u32be(n: int) -> bytes:
    if not isinstance(n, int) or isinstance(n, bool) or not 0 <= n <= _MAX_U32:
        raise MalformedEncodingError(
            "u32 编码值越界", details={"value": n, "max": _MAX_U32}
        )
    return n.to_bytes(4, "big")


def u64be(n: int) -> bytes:
    if not isinstance(n, int) or isinstance(n, bool) or not 0 <= n <= _MAX_U64:
        raise MalformedEncodingError(
            "u64 编码值越界", details={"value": n, "max": _MAX_U64}
        )
    return n.to_bytes(8, "big")


def varint(n: int) -> bytes:
    if not isinstance(n, int) or isinstance(n, bool) or n < 0:
        raise MalformedEncodingError("varint 必须为非负整数", details={"value": n})
    if n < 0xFD:
        return bytes((n,))
    if n <= 0xFFFF:
        return b"\xfd" + n.to_bytes(2, "big")
    if n <= _MAX_U32:
        return b"\xfe" + n.to_bytes(4, "big")
    return b"\xff" + n.to_bytes(8, "big")


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def hash_chain(prefix: bytes, items: Iterable[bytes]) -> bytes:
    """定长迭代哈希链：h0=sha256(prefix)，h_{i+1}=sha256(h_i+item)。空列表合法。"""
    h = sha256(prefix)
    for item in items:
        h = sha256(h + item)
    return h


def tx_root(txids: Iterable[bytes]) -> bytes:
    return hash_chain(_TXROOT_TAG, list(txids))


def witness_root(txs: Iterable[Transaction]) -> bytes:
    return hash_chain(
        _WITROOT_TAG, (sha256(encode_tx_full(tx)) for tx in txs)
    )


def utxo_root(rows: Iterable[bytes]) -> bytes:
    """对**已排序**的 UTXO 行字节求哈希链（排序由调用方负责）。"""
    return hash_chain(_UTXOROOT_TAG, rows)


def utxo_row(txid: bytes, vout: int, amount: int, pubkey: bytes) -> bytes:
    """UTXO 集摘要/快照的单行编码（lexicographic 排序后入根）。"""
    return txid + u32be(vout) + u64be(amount) + pubkey


# ---------------------------------------------------------------------------
# 交易编码
# ---------------------------------------------------------------------------
def encode_tx_body(tx: Transaction) -> bytes:
    """无见证交易体（决定 txid 与签名摘要）。布局见模块 docstring。"""
    parts: list[bytes] = [b"UTXT", u32be(tx.version), varint(len(tx.inputs))]
    for inp in tx.inputs:
        if len(inp.prev.txid) != HASH_SIZE:
            raise MalformedEncodingError(
                "outpoint txid 必须为 32 字节",
                details={"length": len(inp.prev.txid)},
            )
        parts.append(inp.prev.txid)
        parts.append(u32be(inp.prev.vout))
    parts.append(varint(len(tx.outputs)))
    for out in tx.outputs:
        if len(out.pubkey) != PUBKEY_COMPRESSED_SIZE:
            raise MalformedEncodingError(
                "公钥必须为 33 字节压缩格式",
                details={"length": len(out.pubkey)},
            )
        parts.append(u64be(out.amount))
        parts.append(varint(len(out.pubkey)))
        parts.append(out.pubkey)
    parts.append(u64be(tx.fee))
    return b"".join(parts)


def encode_tx_full(tx: Transaction) -> bytes:
    """含见证完整交易。见证数量一致性由内核做语义判定，这里只负责序列化。"""
    parts: list[bytes] = [
        b"UTXW",
        u32be(tx.version),
        varint(len(tx.inputs)),
    ]
    for inp in tx.inputs:
        parts.append(inp.prev.txid)
        parts.append(u32be(inp.prev.vout))
    parts.append(varint(len(tx.outputs)))
    for out in tx.outputs:
        if len(out.pubkey) != PUBKEY_COMPRESSED_SIZE:
            raise MalformedEncodingError(
                "公钥必须为 33 字节压缩格式",
                details={"length": len(out.pubkey)},
            )
        parts.append(u64be(out.amount))
        parts.append(varint(len(out.pubkey)))
        parts.append(out.pubkey)
    parts.append(u64be(tx.fee))
    parts.append(varint(len(tx.witnesses)))
    for wit in tx.witnesses:
        parts.append(varint(len(wit.signature)))
        parts.append(wit.signature)
    return b"".join(parts)


def txid_of(tx: Transaction) -> bytes:
    return sha256(encode_tx_body(tx))


def sighash_of(tx: Transaction) -> bytes:
    """签名消息：固定域分隔标签 + 无见证交易体，防止跨协议复用。"""
    return sha256(_SIGHASH_TAG + encode_tx_body(tx))


# ---------------------------------------------------------------------------
# 块编码
# ---------------------------------------------------------------------------
def _header_payload(h: BlockHeader) -> bytes:
    if len(h.prev_hash) != HASH_SIZE or len(h.tx_root) != HASH_SIZE:
        raise MalformedEncodingError("块头哈希字段必须为 32 字节")
    if len(h.witness_root) != HASH_SIZE:
        raise MalformedEncodingError("witness_root 必须为 32 字节")
    return b"".join(
        [
            u32be(h.version),
            u32be(h.height),
            h.prev_hash,
            u64be(h.timestamp),
            h.tx_root,
            h.witness_root,
        ]
    )


def encode_header(h: BlockHeader) -> bytes:
    return b"UHDR" + _header_payload(h)


def block_id_of(block: Block) -> bytes:
    return sha256(encode_header(block.header))


def encode_block(block: Block) -> bytes:
    parts: list[bytes] = [
        b"UBLK",
        encode_header(block.header),
        u32be(len(block.transactions)),
    ]
    for tx in block.transactions:
        parts.append(encode_tx_full(tx))
    return b"".join(parts)


# ---------------------------------------------------------------------------
# 二进制解码（与上面严格对称；截断/多余字节一律拒绝）
# ---------------------------------------------------------------------------
class _Reader:
    __slots__ = ("buf", "pos")

    def __init__(self, buf: bytes) -> None:
        self.buf = buf
        self.pos = 0

    def take(self, n: int, what: str) -> bytes:
        if self.pos + n > len(self.buf):
            raise MalformedEncodingError(
                f"二进制数据截断：需要 {what} {n} 字节",
                details={"offset": self.pos, "need": n, "have": len(self.buf) - self.pos},
            )
        chunk = self.buf[self.pos : self.pos + n]
        self.pos += n
        return chunk

    def magic(self, expected: bytes) -> None:
        if self.take(len(expected), "magic") != expected:
            raise MalformedEncodingError(
                "魔数不匹配", details={"expected": expected.hex()}
            )

    def u32(self) -> int:
        return int.from_bytes(self.take(4, "u32"), "big")

    def u64(self) -> int:
        return int.from_bytes(self.take(8, "u64"), "big")

    def varint(self) -> int:
        tag = self.take(1, "varint")[0]
        if tag < 0xFD:
            return tag
        if tag == 0xFD:
            return int.from_bytes(self.take(2, "u16"), "big")
        if tag == 0xFE:
            return self.u32()
        return self.u64()

    def varbytes(self, what: str) -> bytes:
        n = self.varint()
        return self.take(n, what)


def _decode_tx(r: _Reader, magic: bytes) -> Transaction:
    r.magic(magic)
    version = r.u32()
    nin = r.varint()
    inputs: list[TxInput] = []
    for _ in range(nin):
        txid = r.take(HASH_SIZE, "txid")
        vout = r.u32()
        inputs.append(TxInput(Outpoint(txid, vout)))
    nout = r.varint()
    outputs: list[TxOutput] = []
    for _ in range(nout):
        amount = r.u64()
        pubkey = r.varbytes("pubkey")
        outputs.append(TxOutput(amount, pubkey))
    fee = r.u64()
    witnesses: list[Witness] = []
    if magic == b"UTXW":
        nwit = r.varint()
        for _ in range(nwit):
            sig = r.varbytes("signature")
            witnesses.append(Witness(sig))
    return Transaction(
        version=version,
        inputs=tuple(inputs),
        outputs=tuple(outputs),
        fee=fee,
        witnesses=tuple(witnesses),
    )


def _decode_header_payload(r: _Reader, *, with_magic: bool = False) -> BlockHeader:
    if with_magic:
        r.magic(b"UHDR")
    version = r.u32()
    height = r.u32()
    prev_hash = r.take(HASH_SIZE, "prev_hash")
    timestamp = r.u64()
    txr = r.take(HASH_SIZE, "tx_root")
    wr = r.take(HASH_SIZE, "witness_root")
    return BlockHeader(
        version=version,
        height=height,
        prev_hash=prev_hash,
        timestamp=timestamp,
        tx_root=txr,
        witness_root=wr,
    )


def decode_block(data: bytes) -> Block:
    r = _Reader(data)
    r.magic(b"UBLK")
    # 注意：完整块字节中 UHDR 魔数只出现一次（见 encode_block）
    header = _decode_header_payload(r, with_magic=True)
    ntx = r.u32()
    txs = tuple(_decode_tx(r, b"UTXW") for _ in range(ntx))
    if r.pos != len(data):
        raise MalformedEncodingError(
            "块编码存在多余尾部字节", details={"extra": len(data) - r.pos}
        )
    return Block(header=header, transactions=txs)


# ---------------------------------------------------------------------------
# JSON 编解码（严格；与二进制共用同一领域对象）
# ---------------------------------------------------------------------------
def _hex_field(obj: dict[str, Any], key: str, length: int) -> bytes:
    if key not in obj:
        raise MalformedEncodingError("JSON 缺少字段", details={"field": key})
    raw = obj[key]
    if not isinstance(raw, str):
        raise MalformedEncodingError(
            "hex 字段必须为字符串", details={"field": key, "type": type(raw).__name__}
        )
    try:
        value = bytes.fromhex(raw)
    except ValueError as exc:
        raise MalformedEncodingError(
            "hex 解码失败", details={"field": key}
        ) from exc
    if len(value) != length:
        raise MalformedEncodingError(
            "hex 字段长度不符",
            details={"field": key, "expect": length, "actual": len(value)},
        )
    return value


def _int_field(obj: dict[str, Any], key: str, *, lo: int, hi: int) -> int:
    if key not in obj:
        raise MalformedEncodingError("JSON 缺少字段", details={"field": key})
    value = obj[key]
    if not isinstance(value, int) or isinstance(value, bool):
        raise MalformedEncodingError(
            "整数字段类型错误",
            details={"field": key, "type": type(value).__name__},
        )
    if not lo <= value <= hi:
        raise MalformedEncodingError(
            "整数字段超可编码范围",
            details={"field": key, "value": value, "lo": lo, "hi": hi},
        )
    return value


def _exact_keys(obj: dict[str, Any], allowed: set[str], what: str) -> None:
    if not isinstance(obj, dict):
        raise MalformedEncodingError(f"{what} 必须为 JSON 对象")
    unknown = set(obj) - allowed
    missing = allowed - set(obj)
    if unknown or missing:
        raise MalformedEncodingError(
            f"{what} 键集合不精确匹配",
            details={"unknown": sorted(unknown), "missing": sorted(missing)},
        )


def tx_from_json(obj: dict[str, Any]) -> Transaction:
    # "txid" 为可选只读派生字段：若给出必须与重算值一致
    _exact_keys(
        obj,
        {"version", "inputs", "outputs", "fee", "witnesses", "txid"},
        "transaction",
    )
    version = _int_field(obj, "version", lo=0, hi=_MAX_U32)
    inputs: list[TxInput] = []
    if not isinstance(obj["inputs"], list):
        raise MalformedEncodingError("inputs 必须为数组")
    for i, raw in enumerate(obj["inputs"]):
        _exact_keys(raw, {"txid", "vout"}, f"inputs[{i}]")
        txid = _hex_field(raw, "txid", HASH_SIZE)
        vout = _int_field(raw, "vout", lo=0, hi=_MAX_U32)
        inputs.append(TxInput(Outpoint(txid, vout)))
    outputs: list[TxOutput] = []
    if not isinstance(obj["outputs"], list):
        raise MalformedEncodingError("outputs 必须为数组")
    for i, raw in enumerate(obj["outputs"]):
        _exact_keys(raw, {"amount", "pubkey"}, f"outputs[{i}]")
        amount = _int_field(raw, "amount", lo=_MIN_I64, hi=_MAX_U64)
        pubkey = _hex_field(raw, "pubkey", PUBKEY_COMPRESSED_SIZE)
        outputs.append(TxOutput(amount, pubkey))
    fee = _int_field(obj, "fee", lo=_MIN_I64, hi=_MAX_U64)
    witnesses: list[Witness] = []
    if not isinstance(obj["witnesses"], list):
        raise MalformedEncodingError("witnesses 必须为数组")
    for i, raw in enumerate(obj["witnesses"]):
        _exact_keys(raw, {"signature"}, f"witnesses[{i}]")
        if not isinstance(raw["signature"], str):
            raise MalformedEncodingError(
                "signature 必须为 hex 字符串", details={"index": i}
            )
        try:
            sig = bytes.fromhex(raw["signature"])
        except ValueError as exc:
            raise MalformedEncodingError(
                "signature hex 解码失败", details={"index": i}
            ) from exc
        witnesses.append(Witness(sig))
    tx = Transaction(
        version=version,
        inputs=tuple(inputs),
        outputs=tuple(outputs),
        fee=fee,
        witnesses=tuple(witnesses),
    )
    if "txid" in obj:
        declared = _hex_field(obj, "txid", HASH_SIZE)
        if declared != txid_of(tx):
            from .errors import HashMismatchError

            raise HashMismatchError(
                "JSON 声明的 txid 与重算结果不一致",
                details={"declared": declared.hex(), "computed": txid_of(tx).hex()},
            )
    return tx


def block_from_json(obj: dict[str, Any]) -> Block:
    # "block_id" 为可选只读派生字段：若给出必须与重算值一致
    _exact_keys(obj, {"header", "transactions", "block_id"}, "block")
    h = obj["header"]
    _exact_keys(
        h,
        {"version", "height", "prev_hash", "timestamp", "tx_root", "witness_root"},
        "header",
    )
    header = BlockHeader(
        version=_int_field(h, "version", lo=0, hi=_MAX_U32),
        height=_int_field(h, "height", lo=0, hi=_MAX_U32),
        prev_hash=_hex_field(h, "prev_hash", HASH_SIZE),
        timestamp=_int_field(h, "timestamp", lo=0, hi=_MAX_U64),
        tx_root=_hex_field(h, "tx_root", HASH_SIZE),
        witness_root=_hex_field(h, "witness_root", HASH_SIZE),
    )
    if not isinstance(obj["transactions"], list):
        raise MalformedEncodingError("transactions 必须为数组")
    txs = tuple(tx_from_json(tx) for tx in obj["transactions"])
    block = Block(header=header, transactions=txs)
    if "block_id" in obj:
        declared = _hex_field(obj, "block_id", HASH_SIZE)
        if declared != block_id_of(block):
            from .errors import HashMismatchError

            raise HashMismatchError(
                "JSON 声明的 block_id 与重算结果不一致",
                details={
                    "declared": declared.hex(),
                    "computed": block_id_of(block).hex(),
                },
            )
    return block


def tx_to_json(tx: Transaction) -> dict[str, Any]:
    return {
        "version": tx.version,
        "inputs": [inp.prev.to_dict() for inp in tx.inputs],
        "outputs": [
            {"amount": out.amount, "pubkey": out.pubkey.hex()} for out in tx.outputs
        ],
        "fee": tx.fee,
        "witnesses": [{"signature": w.signature.hex()} for w in tx.witnesses],
        "txid": txid_of(tx).hex(),
    }


def block_to_json(block: Block) -> dict[str, Any]:
    return {
        "block_id": block_id_of(block).hex(),
        "header": {
            "version": block.header.version,
            "height": block.header.height,
            "prev_hash": block.header.prev_hash.hex(),
            "timestamp": block.header.timestamp,
            "tx_root": block.header.tx_root.hex(),
            "witness_root": block.header.witness_root.hex(),
        },
        "transactions": [tx_to_json(tx) for tx in block.transactions],
    }


def dumps_canonical(obj: Any) -> bytes:
    """夹具/日志使用的规范 JSON（sort_keys、无多余空白、LF 结尾由调用方处理）。"""
    return (
        json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    ).encode("utf-8")
