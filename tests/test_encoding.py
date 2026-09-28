"""Encoding tests: fixed wire format, canonical hashes, structural rejects."""

import hashlib

import pytest

from utxo_ledger.encoding import (
    Block,
    Outpoint,
    Transaction,
    TxInput,
    TxOutput,
    compute_block_hash,
    compute_txid,
    decode_block,
    decode_tx,
    encode_block,
    encode_tx,
    merkle_root,
    tx_sighash,
)
from utxo_ledger.errors import ErrorCode, LedgerError
from utxo_ledger.protocol import SIGHASH_TAG, TX_TAG, ZERO_HASH

PK = bytes(range(32))
PK2 = bytes(range(1, 33))


def _tx(sigs=(b"",), values=(100,), version=1):
    return Transaction(
        version=version,
        inputs=tuple(
            TxInput(Outpoint(ZERO_HASH, i), s) for i, s in enumerate(sigs)
        ),
        outputs=tuple(TxOutput(v, PK) for v in values),
    )


def test_tx_roundtrip_is_byte_identical():
    tx = _tx(sigs=(b"\x01" * 64, b"\x02" * 64), values=(1, 2, 3))
    raw = encode_tx(tx)
    back = decode_tx(raw)
    assert back == tx
    assert encode_tx(back) == raw  # canonical: single representation


def test_txid_is_sha256_of_full_encoding_and_covers_signatures():
    tx = _tx(sigs=(b"",))
    assert tx.txid == hashlib.sha256(encode_tx(tx)).digest()
    tx2 = tx.with_signature(0, b"\xaa" * 64)
    assert compute_txid(tx2) != tx.txid  # changing a sig changes the txid


def test_sighash_independent_of_signature_but_dependent_on_outputs():
    tx = _tx(sigs=(b"",))
    signed = tx.with_signature(0, b"\xaa" * 64)
    assert tx_sighash(tx) == tx_sighash(signed)
    other = Transaction(tx.version, tx.inputs, (TxOutput(101, PK),))
    assert tx_sighash(other) != tx_sighash(tx)
    # domain separation: sighash is not the plain txid
    assert tx_sighash(tx) != tx.txid
    assert hashlib.sha256(SIGHASH_TAG + TX_TAG + encode_tx(tx)[len(TX_TAG):]).digest() != tx.txid


def test_decode_rejects_trailing_bytes():
    raw = encode_tx(_tx()) + b"\x00"
    with pytest.raises(LedgerError) as ei:
        decode_tx(raw)
    assert ei.value.code is ErrorCode.INVALID_ENCODING
    assert ei.value.category.value == "input"


def test_decode_rejects_bad_tag_and_truncation():
    raw = bytearray(encode_tx(_tx()))
    raw[0] ^= 0xFF
    with pytest.raises(LedgerError) as ei:
        decode_tx(bytes(raw))
    assert ei.value.code is ErrorCode.MALFORMED
    with pytest.raises(LedgerError) as ei2:
        decode_tx(encode_tx(_tx())[:-5])
    assert ei2.value.code is ErrorCode.MALFORMED


def test_pubkey_must_be_32_bytes_structural():
    bad = Transaction(
        version=1,
        inputs=(TxInput(Outpoint(ZERO_HASH, 1), b""),),
        outputs=(TxOutput(10, b"\x01" * 31),),
    )
    with pytest.raises(LedgerError) as ei:
        decode_tx(encode_tx(bad))
    assert ei.value.code is ErrorCode.BAD_PUBLIC_KEY


def test_block_roundtrip_and_merkle_ordering():
    t1 = _tx()
    t2 = _tx(values=(200,))
    blk = Block(1, 7, ZERO_HASH, (t1, t2))
    raw = encode_block(blk)
    back = decode_block(raw)
    assert back == blk
    assert compute_block_hash(blk) == blk.hash
    # Merkle root commits to tx order: swapping changes the block hash.
    swapped = Block(1, 7, ZERO_HASH, (t2, t1))
    assert compute_block_hash(swapped) != blk.hash


def test_merkle_duplicate_odd_level():
    a, b = hashlib.sha256(b"a").digest(), hashlib.sha256(b"b").digest()
    single = merkle_root([a])
    assert single == a
    three = merkle_root([a, b, a])
    assert isinstance(three, bytes) and len(three) == 32


def test_empty_block_is_structurally_decodable_but_semantically_rejected():
    blk = Block(1, 1, ZERO_HASH, ())
    raw = encode_block(blk)
    back = decode_block(raw)  # structural decode allows n_tx=0
    assert back.transactions == ()
    from utxo_ledger.kernel import InMemoryChainView, validate_block

    with pytest.raises(LedgerError) as ei:
        validate_block(back, InMemoryChainView())
    assert ei.value.code is ErrorCode.EMPTY_BLOCK
    assert ei.value.category.value == "input"
