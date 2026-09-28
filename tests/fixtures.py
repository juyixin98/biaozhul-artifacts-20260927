"""Reusable synthetic fixtures for the test-asset ledger.

Deterministic, local-only: keys are derived from named seeds, no network and no
real accounts. ``FixtureBuilder`` produces canonical encoded blocks/transactions
using the production *encoding* layer (that is the wire-format library under
test), while expected state-transition answers come from the independent
oracle in :mod:`tests.oracle`, which has its own copy of the spec.
"""

from __future__ import annotations

import hashlib

from utxo_ledger.crypto import KeyPair
from utxo_ledger.encoding import (
    Block,
    Outpoint,
    Transaction,
    TxInput,
    TxOutput,
    encode_block,
)
from utxo_ledger.protocol import SUPPORTED_BLOCK_VERSION, SUPPORTED_TX_VERSION, ZERO_HASH


def named_key(name: str) -> KeyPair:
    """Deterministic Ed25519 keypair from a human label."""
    seed = hashlib.sha256(b"fixture-key/v1/" + name.encode()).digest()
    return KeyPair.from_seed(seed)


def coinbase_tx(height: int, outputs: list[tuple[int, bytes]], *, version: int = SUPPORTED_TX_VERSION) -> Transaction:
    """Coinbase: one null input (vout = height), no signature."""
    return Transaction(
        version=version,
        inputs=(TxInput(Outpoint(ZERO_HASH, height), b""),),
        outputs=tuple(TxOutput(v, pk) for v, pk in outputs),
    )


def transfer_tx(
    inputs: list[tuple[Outpoint, bytes]],
    outputs: list[tuple[int, bytes]],
    signers: dict[int, KeyPair],
    *,
    version: int = SUPPORTED_TX_VERSION,
    raw_signatures: dict[int, bytes] | None = None,
) -> Transaction:
    """Build a signed transfer transaction.

    ``inputs`` maps to ``(outpoint, pubkey_of_funding_output)``; ``signers``
    maps input index -> keypair that must sign. Signatures cover the canonical
    sighash, computed here via the production encoding helper (fixture tooling
    may use it; the oracle recomputes its own digest independently).
    """
    from utxo_ledger.encoding import tx_sighash

    tx = Transaction(
        version=version,
        inputs=tuple(TxInput(op, b"") for op, _pk in inputs),
        outputs=tuple(TxOutput(v, pk) for v, pk in outputs),
    )
    digest = tx_sighash(tx)
    signed_inputs = []
    raw_signatures = raw_signatures or {}
    for i, (op, _pk) in enumerate(inputs):
        if i in raw_signatures:
            sig = raw_signatures[i]
        else:
            sig = signers[i].sign(digest)
        signed_inputs.append(TxInput(op, sig))
    return Transaction(
        version=version,
        inputs=tuple(signed_inputs),
        outputs=tx.outputs,
    )


def make_block(height: int, prev_hash: bytes, txs: list[Transaction], *,
               version: int = SUPPORTED_BLOCK_VERSION) -> Block:
    return Block(
        version=version,
        height=height,
        prev_hash=prev_hash,
        transactions=tuple(txs),
    )


def make_genesis_block(
    coinbase_outputs: list[tuple[int, bytes]],
    *,
    prev_hash: bytes = ZERO_HASH,
) -> Block:
    return make_block(1, prev_hash, [coinbase_tx(1, coinbase_outputs)])


class FixtureBuilder:
    """Stateful helper: tracks the tip hash to chain blocks together."""

    def __init__(self, prev_hash: bytes = ZERO_HASH) -> None:
        self.prev_hash = prev_hash
        self.height = 0
        self.raw_blocks: list[bytes] = []

    def append(self, txs: list[Transaction], *, height: int | None = None) -> Block:
        self.height += 1
        h = height if height is not None else self.height
        block = make_block(h, self.prev_hash, txs)
        self.prev_hash = block.hash
        self.raw_blocks.append(encode_block(block))
        return block
