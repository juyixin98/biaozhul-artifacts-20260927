"""可复用夹具构造器 (synthetic fixture builders)。

只构造**结构合法的本地合成数据**：确定性测试密钥（crypto.fixture_private_key）、
整数金额、固定编码签名。不联网、不接触任何生产身份。

构造器不调用内核共识规则——它只把交易拼出来；合法/非法由测试或独立 oracle 判定。
这样同一套构造器既能造有效链，也能造双花/篡改等攻击变体。
"""
from __future__ import annotations

from typing import Sequence

from . import crypto, encoding
from .encoding import (
    Block,
    BlockHeader,
    Outpoint,
    PROTOCOL_VERSION,
    Transaction,
    TxInput,
    TxOutput,
    Witness,
    block_id_of,
    tx_root,
    txid_of,
    witness_root,
)

FIXTURE_TIMESTAMP = 1_700_000_000  # 固定时间戳，夹具可复现


class KeyRing:
    """确定性测试密钥环：index -> (私钥, 33B 压缩公钥)。"""

    def __init__(self) -> None:
        self._priv: dict[int, object] = {}
        self._pub: dict[int, bytes] = {}

    def pub(self, index: int) -> bytes:
        if index not in self._pub:
            priv, pub = crypto.fixture_keypair(index)
            self._priv[index] = priv
            self._pub[index] = pub
        return self._pub[index]

    def priv(self, index: int):
        self.pub(index)
        return self._priv[index]


def make_output(amount: int, pubkey: bytes) -> TxOutput:
    return TxOutput(amount=amount, pubkey=pubkey)


def issue_tx(outputs: Sequence[tuple[int, bytes]], *, version: int = PROTOCOL_VERSION) -> Transaction:
    """构造无输入发行交易（仅可用于 genesis）。见证数=输入数=0。"""
    return Transaction(
        version=version,
        inputs=(),
        outputs=tuple(TxOutput(amount=a, pubkey=pk) for a, pk in outputs),
        fee=0,
        witnesses=(),
    )


def unsigned_tx(
    inputs: Sequence[Outpoint],
    outputs: Sequence[TxOutput],
    *,
    fee: int = 0,
    version: int = PROTOCOL_VERSION,
) -> Transaction:
    """构造尚未签名的交易（占位空见证，供"漏签/错签"变体使用）。"""
    return Transaction(
        version=version,
        inputs=tuple(TxInput(prev=o) for o in inputs),
        outputs=tuple(outputs),
        fee=fee,
        witnesses=tuple(Witness(signature=b"") for _ in inputs),
    )


def sign_tx(
    tx: Transaction,
    *,
    owner_privkeys: Sequence[object],
) -> Transaction:
    """按输入顺序用各被花费输出的所有者私钥签名（固定 sighash 摘要）。"""
    if len(owner_privkeys) != len(tx.inputs):
        raise ValueError("owner_privkeys 数量必须与输入数量一致")
    msg = encoding.sighash_of(tx)
    wits = tuple(Witness(signature=crypto.sign(k, msg)) for k in owner_privkeys)
    return Transaction(
        version=tx.version,
        inputs=tx.inputs,
        outputs=tx.outputs,
        fee=tx.fee,
        witnesses=wits,
    )


def with_witnesses(tx: Transaction, signatures: Sequence[bytes]) -> Transaction:
    """直接替换见证（用于签名篡改/空签名/DER 损坏等变体）。"""
    return Transaction(
        version=tx.version,
        inputs=tx.inputs,
        outputs=tx.outputs,
        fee=tx.fee,
        witnesses=tuple(Witness(signature=s) for s in signatures),
    )


def make_block(
    txs: Sequence[Transaction],
    *,
    height: int,
    prev_hash: bytes,
    timestamp: int = FIXTURE_TIMESTAMP,
    version: int = PROTOCOL_VERSION,
    tx_root_override: bytes | None = None,
    witness_root_override: bytes | None = None,
) -> Block:
    """用交易列表计算根并组装块。override 用于构造根不匹配攻击变体。"""
    ids = [txid_of(t) for t in txs]
    header = BlockHeader(
        version=version,
        height=height,
        prev_hash=prev_hash,
        timestamp=timestamp,
        tx_root=tx_root_override or tx_root(ids),
        witness_root=witness_root_override or witness_root(txs),
    )
    return Block(header=header, transactions=tuple(txs))


def genesis_block(txs: Sequence[Transaction], *, timestamp: int = FIXTURE_TIMESTAMP) -> Block:
    return make_block(
        txs, height=0, prev_hash=encoding.ZERO_HASH, timestamp=timestamp
    )


def next_block(
    txs: Sequence[Transaction],
    prev: Block,
    *,
    timestamp: int = FIXTURE_TIMESTAMP,
) -> Block:
    return make_block(
        txs,
        height=prev.header.height + 1,
        prev_hash=block_id_of(prev),
        timestamp=timestamp,
    )


def block_to_fixture_json(block: Block) -> dict:
    return encoding.block_to_json(block)
