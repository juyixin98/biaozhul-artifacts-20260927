"""SQLite storage tests: indexing, spend history, durability, atomic commit."""

from __future__ import annotations

from utxo_ledger.encoding import Outpoint, encode_block
from utxo_ledger.errors import ErrorCode
from utxo_ledger.node import LedgerNode
from utxo_ledger.storage import SqliteStore

from tests.fixtures import FixtureBuilder, coinbase_tx, named_key, transfer_tx


def _two_block_raw():
    alice, bob = named_key("alice"), named_key("bob")
    fb = FixtureBuilder()
    b1 = fb.append([coinbase_tx(1, [(1_000_000, alice.public_bytes)])])
    tx = transfer_tx(
        [(Outpoint(b1.transactions[0].txid, 0), alice.public_bytes)],
        [(400_000, bob.public_bytes), (599_000, alice.public_bytes)],
        {0: alice},
    )
    fb.append([coinbase_tx(2, [(1_001_000, alice.public_bytes)]), tx])
    return fb, alice, bob


def test_utxo_index_and_spend_history():
    fb, alice, bob = _two_block_raw()
    store = SqliteStore(":memory:")
    node = LedgerNode(store)
    for i, raw in enumerate(fb.raw_blocks, 1):
        assert node.submit_raw_block(raw, i).accepted

    # The spent genesis outpoint is gone from utxos but present in history.
    assert store.get_utxo(_genesis_txid(fb), 0) is None
    hist = store.spend_history()
    assert len(hist) == 1
    assert hist[0]["value"] == 1_000_000
    assert hist[0]["spent_height"] == 2

    # Index supports pubkey balance lookups.
    assert store.balance(bob.public_bytes) == 400_000
    assert store.balance(alice.public_bytes) == 599_000 + 1_001_000
    assert store.utxo_count() == 3


def _genesis_txid(fb) -> bytes:
    from utxo_ledger.encoding import decode_block

    return decode_block(fb.raw_blocks[0]).transactions[0].txid


def test_block_and_tx_raw_roundtrip_for_replay():
    fb, _, _ = _two_block_raw()
    store = SqliteStore(":memory:")
    node = LedgerNode(store)
    for i, raw in enumerate(fb.raw_blocks, 1):
        node.submit_raw_block(raw, i)
    assert store.get_block_raw(1) == fb.raw_blocks[0]
    assert store.get_block_raw(2) == fb.raw_blocks[1]
    assert store.get_block_info(2)["fee_total"] == 1_000


def test_invalid_block_leaves_database_unchanged(tmp_path):
    db = tmp_path / "ledger.db"
    store = SqliteStore(str(db))
    node = LedgerNode(store)
    fb, alice, _ = _two_block_raw()
    assert node.submit_raw_block(fb.raw_blocks[0], 1).accepted
    tip_before = (store.tip_height, store.tip_hash)
    count_before = store.utxo_count()

    # Block with a zero-value output at height 2 must be rejected atomically.
    from utxo_ledger.encoding import Block
    bad = Block(
        1, 2, store.tip_hash,
        (
            coinbase_tx(2, [(1_000_000, alice.public_bytes)]),
            transfer_tx(
                [(Outpoint(_genesis_txid(fb), 0), alice.public_bytes)],
                [(0, alice.public_bytes)],
                {0: alice},
            ),
        ),
    )
    res = node.submit_raw_block(encode_block(bad), 2)
    assert not res.accepted
    assert res.error["code"] == ErrorCode.ZERO_VALUE_OUTPUT.value

    # Reopen the file to prove nothing partial hit disk.
    store.close()
    reopened = SqliteStore(str(db))
    assert (reopened.tip_height, reopened.tip_hash) == tip_before
    assert reopened.utxo_count() == count_before
    assert reopened.get_block_raw(2) is None
    assert len(reopened.spend_history()) == 0
    reopened.close()


def test_duplicate_block_rejected(tmp_path):
    db = tmp_path / "ledger.db"
    store = SqliteStore(str(db))
    node = LedgerNode(store)
    fb, _, _ = _two_block_raw()
    assert node.submit_raw_block(fb.raw_blocks[0], 1).accepted
    assert node.submit_raw_block(fb.raw_blocks[0], 2).accepted is False
    res = node.submit_raw_block(fb.raw_blocks[0], 3)
    # Same height already present -> height mismatch or duplicate block, both
    # STATE; height check fires first (tip advanced to 1, block still says 1).
    assert res.error["category"] == "state"
    assert res.error["code"] in {
        ErrorCode.BLOCK_HEIGHT_INVALID.value,
        ErrorCode.DUPLICATE_BLOCK.value,
    }
    store.close()
