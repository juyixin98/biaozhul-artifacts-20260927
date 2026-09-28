"""Canonical fixed binary encoding, content hashes and structural decoding.

Wire format (little-endian, no version negotiation beyond the u32 version):

Transaction::

    TX_TAG (11 bytes) | version u32 | n_in u16 | n_out u16
    input  * n_in : txid(32) | vout u32 | sig_len u16 | sig(sig_len)
    output * n_out: value u64 | pk_len u16 | pk(pk_len)

Block::

    BLOCK_TAG (13 bytes) | version u32 | height u64 | prev_hash(32)
    | n_tx u32 | transactions concatenated

Content identifiers (all SHA-256, domain-separated):

* ``txid``      = SHA256(TX_TAG | body)              -- commits to sigs as well.
* ``sighash``   = SHA256(SIGHASH_TAG | TX_TAG | body-without-signature-fields)
                  one shared digest per transaction; every input must carry a
                  signature valid under the pubkey of the output it spends.
* block hash   = SHA256(BLOCK_TAG | version | height | prev_hash
                  | merkle_root | n_tx), merkle root commits to txids in order.

This module performs *structural* validation only (shapes, lengths, limits,
exact-consumption decode). Semantic validation (spendability, amounts, order,
signatures) is the kernel's job, so failures can be attributed per transaction.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass, field, replace

from .errors import ErrorCode, LedgerError
from .protocol import (
    BLOCK_TAG,
    GENESIS_PREV_HASH,
    HASH_LEN,
    MAX_BLOCK_RAW,
    MAX_BLOCK_TXS,
    MAX_PUBKEY_BYTES,
    MAX_SIGNATURE_BYTES,
    MAX_TX_INPUTS,
    MAX_TX_OUTPUTS,
    PUBLIC_KEY_LEN,
    SIGHASH_TAG,
    SUPPORTED_BLOCK_VERSION,
    SUPPORTED_TX_VERSION,
    TXID_LEN,
    TX_TAG,
)


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Outpoint:
    txid: bytes
    vout: int

    def key(self) -> tuple[bytes, int]:
        return (self.txid, self.vout)

    @property
    def is_null(self) -> bool:
        return self.txid == GENESIS_PREV_HASH


@dataclass(frozen=True)
class TxInput:
    outpoint: Outpoint
    signature: bytes = b""


@dataclass(frozen=True)
class TxOutput:
    value: int
    pubkey: bytes


@dataclass(frozen=True)
class Transaction:
    version: int
    inputs: tuple[TxInput, ...]
    outputs: tuple[TxOutput, ...]
    # Cached identity, not part of encoding.
    _txid: bytes = field(default=b"", compare=False, repr=False)

    @property
    def txid(self) -> bytes:
        if not self._txid:
            object.__setattr__(self, "_txid", compute_txid(self))
        return self._txid

    @property
    def is_coinbase(self) -> bool:
        return len(self.inputs) == 1 and self.inputs[0].outpoint.is_null

    def coinbase_height(self) -> int | None:
        return self.inputs[0].outpoint.vout if self.is_coinbase else None

    def with_signature(self, index: int, signature: bytes) -> "Transaction":
        inp = replace(self.inputs[index], signature=signature)
        inputs = self.inputs[:index] + (inp,) + self.inputs[index + 1 :]
        return replace(self, inputs=inputs, _txid=b"")


@dataclass(frozen=True)
class Block:
    version: int
    height: int
    prev_hash: bytes
    transactions: tuple[Transaction, ...]

    @property
    def hash(self) -> bytes:
        return compute_block_hash(self)


# --------------------------------------------------------------------------- #
# Low-level helpers
# --------------------------------------------------------------------------- #
def _sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def _pack_u16(n: int) -> bytes:
    return struct.pack("<H", n)


def _unpack(reader: "_Reader", fmt: str, size: int, what: str):
    if len(reader.buf) - reader.pos < size:
        raise LedgerError(
            ErrorCode.MALFORMED, f"truncated encoding while reading {what}"
        )
    return struct.unpack(fmt, reader.read(size))[0]


class _Reader:
    def __init__(self, buf: bytes) -> None:
        self.buf = buf
        self.pos = 0

    def read(self, n: int) -> bytes:
        if n < 0 or self.pos + n > len(self.buf):
            raise LedgerError(ErrorCode.MALFORMED, "truncated encoding")
        chunk = self.buf[self.pos : self.pos + n]
        self.pos += n
        return chunk

    def take(self, n: int, what: str) -> bytes:
        if len(self.buf) - self.pos < n:
            raise LedgerError(ErrorCode.MALFORMED, f"truncated encoding while reading {what}")
        return self.read(n)

    def u16(self, what: str) -> int:
        return _unpack(self, "<H", 2, what)

    def u32(self, what: str) -> int:
        return _unpack(self, "<I", 4, what)

    def u64(self, what: str) -> int:
        return _unpack(self, "<Q", 8, what)

    def tag(self, expected: bytes, what: str) -> None:
        got = self.take(len(expected), what)
        if got != expected:
            raise LedgerError(
                ErrorCode.MALFORMED,
                f"bad {what} domain tag: expected {expected!r}, got {got!r}",
            )

    def must_be_exhausted(self) -> None:
        if self.pos != len(self.buf):
            raise LedgerError(
                ErrorCode.INVALID_ENCODING,
                f"{len(self.buf) - self.pos} trailing byte(s) after decoded object",
            )

    def var_bytes(self, max_len: int, what: str) -> bytes:
        n = self.u16(f"{what} length")
        if n > max_len:
            raise LedgerError(
                ErrorCode.MALFORMED,
                f"{what} length {n} exceeds structural bound {max_len}",
            )
        return self.take(n, what)


# --------------------------------------------------------------------------- #
# Transaction encode
# --------------------------------------------------------------------------- #
def _encode_tx_body(tx: Transaction, include_signatures: bool) -> bytes:
    if not (0 <= tx.version <= 0xFFFFFFFF):
        raise LedgerError(ErrorCode.BAD_VERSION, "tx version out of u32 range")
    n_in, n_out = len(tx.inputs), len(tx.outputs)
    if n_in > 0xFFFF or n_out > 0xFFFF:
        raise LedgerError(ErrorCode.MALFORMED, "too many tx inputs/outputs to encode")
    parts = [TX_TAG, struct.pack("<I", tx.version), _pack_u16(n_in), _pack_u16(n_out)]
    for inp in tx.inputs:
        if len(inp.outpoint.txid) != TXID_LEN:
            raise LedgerError(ErrorCode.BAD_TXID, "txid must be 32 bytes")
        if not (0 <= inp.outpoint.vout <= 0xFFFFFFFF):
            raise LedgerError(ErrorCode.MALFORMED, "vout out of u32 range")
        parts.append(inp.outpoint.txid)
        parts.append(struct.pack("<I", inp.outpoint.vout))
        if include_signatures:
            if len(inp.signature) > 0xFFFF:
                raise LedgerError(ErrorCode.BAD_SIGNATURE, "signature too long")
            parts.append(_pack_u16(len(inp.signature)))
            parts.append(inp.signature)
    for out in tx.outputs:
        if not (0 <= out.value <= 0xFFFFFFFFFFFFFFFF):
            raise LedgerError(
                ErrorCode.VALUE_OUT_OF_RANGE, "output value outside u64"
            )
        if not (0 < len(out.pubkey) <= 0xFFFF):
            raise LedgerError(ErrorCode.BAD_PUBLIC_KEY, "empty or oversized pubkey")
        parts.append(struct.pack("<Q", out.value))
        parts.append(_pack_u16(len(out.pubkey)))
        parts.append(out.pubkey)
    return b"".join(parts)


def encode_tx(tx: Transaction) -> bytes:
    return _encode_tx_body(tx, include_signatures=True)


def tx_sighash(tx: Transaction) -> bytes:
    """Digest every input signs. Independent of signature slot contents."""
    body = _encode_tx_body(tx, include_signatures=False)
    # Strip the TX_TAG from the body because the sighash gets its own domain tag,
    # then prepend SIGHASH_TAG + TX_TAG to keep domain separation explicit.
    assert body.startswith(TX_TAG)
    return _sha256(SIGHASH_TAG + body)


def compute_txid(tx: Transaction) -> bytes:
    return _sha256(encode_tx(tx))


def decode_tx(raw: bytes) -> Transaction:
    """Structural decode of one transaction (no semantic rules)."""
    if not isinstance(raw, (bytes, bytearray)):
        raise LedgerError(ErrorCode.MALFORMED, "raw tx must be bytes")
    r = _Reader(bytes(raw))
    r.tag(TX_TAG, "transaction")
    version = r.u32("tx version")
    n_in = r.u16("input count")
    n_out = r.u16("output count")
    if n_in > MAX_TX_INPUTS:
        raise LedgerError(
            ErrorCode.TOO_MANY_INPUTS,
            f"{n_in} inputs exceeds limit {MAX_TX_INPUTS}",
        )
    if n_out > MAX_TX_OUTPUTS:
        raise LedgerError(
            ErrorCode.TOO_MANY_OUTPUTS,
            f"{n_out} outputs exceeds limit {MAX_TX_OUTPUTS}",
        )
    if n_in == 0:
        raise LedgerError(ErrorCode.MALFORMED, "transaction has no inputs")
    if n_out == 0:
        raise LedgerError(ErrorCode.MALFORMED, "transaction has no outputs")

    inputs: list[TxInput] = []
    for _ in range(n_in):
        txid = r.take(TXID_LEN, "input txid")
        vout = r.u32("input vout")
        sig = r.var_bytes(MAX_SIGNATURE_BYTES, "signature")
        inputs.append(TxInput(Outpoint(txid, vout), sig))

    outputs: list[TxOutput] = []
    for _ in range(n_out):
        value = r.u64("output value")
        pk = r.var_bytes(MAX_PUBKEY_BYTES, "pubkey")
        if len(pk) != PUBLIC_KEY_LEN:
            raise LedgerError(
                ErrorCode.BAD_PUBLIC_KEY,
                f"pubkey must be {PUBLIC_KEY_LEN} bytes, got {len(pk)}",
            )
        outputs.append(TxOutput(value, pk))
    r.must_be_exhausted()
    return Transaction(version, tuple(inputs), tuple(outputs))


# --------------------------------------------------------------------------- #
# Merkle / block
# --------------------------------------------------------------------------- #
def merkle_root(txids: tuple[bytes, ...] | list[bytes]) -> bytes:
    if not txids:
        return GENESIS_PREV_HASH
    level = list(txids)
    while len(level) > 1:
        if len(level) % 2 == 1:
            level.append(level[-1])
        level = [
            _sha256(level[i] + level[i + 1]) for i in range(0, len(level), 2)
        ]
    return level[0]


def encode_block_header_parts(
    version: int,
    height: int,
    prev_hash: bytes,
    transactions: tuple[Transaction, ...] | list[Transaction],
) -> bytes:
    if len(prev_hash) != HASH_LEN:
        raise LedgerError(ErrorCode.BAD_HEADER, "prev_hash must be 32 bytes")
    root = merkle_root([tx.txid for tx in transactions])
    n = len(transactions)
    if not (0 <= version <= 0xFFFFFFFF):
        raise LedgerError(ErrorCode.BAD_VERSION, "block version out of u32 range")
    if not (0 <= height <= 0xFFFFFFFFFFFFFFFF):
        raise LedgerError(ErrorCode.BAD_HEADER, "height out of u64 range")
    if not (0 <= n <= 0xFFFFFFFF):
        raise LedgerError(ErrorCode.TOO_MANY_TXS, "tx count out of u32 range")
    return b"".join(
        [
            BLOCK_TAG,
            struct.pack("<I", version),
            struct.pack("<Q", height),
            prev_hash,
            root,
            struct.pack("<I", n),
        ]
    )


def compute_block_hash(block: "Block") -> bytes:
    return _sha256(
        encode_block_header_parts(
            block.version, block.height, block.prev_hash, block.transactions
        )
    )


def encode_block(block: "Block") -> bytes:
    if len(block.transactions) > 0xFFFF:
        raise LedgerError(ErrorCode.TOO_MANY_TXS, "too many txs to encode")
    parts = [
        BLOCK_TAG,
        struct.pack("<I", block.version),
        struct.pack("<Q", block.height),
        block.prev_hash,
        struct.pack("<I", len(block.transactions)),
    ]
    for tx in block.transactions:
        parts.append(encode_tx(tx))
    return b"".join(parts)


def decode_block(raw: bytes) -> Block:
    """Structural decode of a block; applies size/count resource limits."""
    if not isinstance(raw, (bytes, bytearray)):
        raise LedgerError(ErrorCode.MALFORMED, "raw block must be bytes")
    raw = bytes(raw)
    if len(raw) > MAX_BLOCK_RAW:
        raise LedgerError(
            ErrorCode.BLOCK_TOO_LARGE,
            f"block is {len(raw)} bytes, limit {MAX_BLOCK_RAW}",
            details={"size": len(raw), "limit": MAX_BLOCK_RAW},
        )
    r = _Reader(raw)
    r.tag(BLOCK_TAG, "block")
    version = r.u32("block version")
    height = r.u64("block height")
    prev_hash = r.take(HASH_LEN, "prev_hash")
    n_tx = r.u32("tx count")
    if n_tx > MAX_BLOCK_TXS:
        raise LedgerError(
            ErrorCode.TOO_MANY_TXS,
            f"{n_tx} transactions exceeds limit {MAX_BLOCK_TXS}",
        )
    # Decode transactions sequentially from the shared stream.
    txs: list[Transaction] = []
    for _ in range(n_tx):
        before = r.pos
        tx = _decode_tx_from(r)
        assert r.pos > before
        txs.append(tx)
    r.must_be_exhausted()
    return Block(version, height, prev_hash, tuple(txs))


def _decode_tx_from(r: "_Reader") -> Transaction:
    """Decode one transaction that starts at the reader's current position."""
    r.tag(TX_TAG, "transaction")
    version = r.u32("tx version")
    n_in = r.u16("input count")
    n_out = r.u16("output count")
    if n_in > MAX_TX_INPUTS:
        raise LedgerError(
            ErrorCode.TOO_MANY_INPUTS,
            f"{n_in} inputs exceeds limit {MAX_TX_INPUTS}",
        )
    if n_out > MAX_TX_OUTPUTS:
        raise LedgerError(
            ErrorCode.TOO_MANY_OUTPUTS,
            f"{n_out} outputs exceeds limit {MAX_TX_OUTPUTS}",
        )
    if n_in == 0:
        raise LedgerError(ErrorCode.MALFORMED, "transaction has no inputs")
    if n_out == 0:
        raise LedgerError(ErrorCode.MALFORMED, "transaction has no outputs")
    inputs: list[TxInput] = []
    for _ in range(n_in):
        txid = r.take(TXID_LEN, "input txid")
        vout = r.u32("input vout")
        sig = r.var_bytes(MAX_SIGNATURE_BYTES, "signature")
        inputs.append(TxInput(Outpoint(txid, vout), sig))
    outputs: list[TxOutput] = []
    for _ in range(n_out):
        value = r.u64("output value")
        pk = r.var_bytes(MAX_PUBKEY_BYTES, "pubkey")
        if len(pk) != PUBLIC_KEY_LEN:
            raise LedgerError(
                ErrorCode.BAD_PUBLIC_KEY,
                f"pubkey must be {PUBLIC_KEY_LEN} bytes, got {len(pk)}",
            )
        outputs.append(TxOutput(value, pk))
    return Transaction(version, tuple(inputs), tuple(outputs))


# --------------------------------------------------------------------------- #
# Version hooks used by the kernel
# --------------------------------------------------------------------------- #
def check_supported_versions(block: Block) -> None:
    if block.version != SUPPORTED_BLOCK_VERSION:
        raise LedgerError(
            ErrorCode.BAD_VERSION,
            f"unsupported block version {block.version}",
        )
    for i, tx in enumerate(block.transactions):
        if tx.version != SUPPORTED_TX_VERSION:
            raise LedgerError(
                ErrorCode.BAD_VERSION,
                f"unsupported tx version {tx.version}",
                tx_index=i,
            )
