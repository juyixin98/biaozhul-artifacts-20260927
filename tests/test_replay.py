"""Integration tests for offline replay and structured run logs."""

from __future__ import annotations

import json
import os

from utxo_ledger.encoding import Outpoint, encode_block
from utxo_ledger.node import LedgerNode
from utxo_ledger.replay import replay_chain, replay_fixture_file
from utxo_ledger.runlog import RunLogger
from utxo_ledger.storage import SqliteStore

from tests.fixtures import FixtureBuilder, coinbase_tx, named_key, transfer_tx

FIXTURE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fixtures",
    "cases.jsonl",
)


def test_fixture_file_replays_all_cases(tmp_path):
    report = replay_fixture_file(FIXTURE_FILE, str(tmp_path / "logs"))
    assert report.failed == 0
    assert report.passed >= 8
    # Rejection verdicts carry the concrete category, not just accept bool.
    rejected = [c for c in report.cases if not c.accepted]
    assert {c.actual_code for c in rejected} >= {
        "double_spend",
        "duplicate_input",
        "zero_value_output",
        "sig_tampered",
    }
    assert all(c.state_unchanged_on_reject for c in rejected)


def test_chain_replay_rebuilds_identical_tip(tmp_path):
    src_path = str(tmp_path / "src.db")
    store = SqliteStore(src_path)
    node = LedgerNode(store)
    alice, bob = named_key("alice"), named_key("bob")
    fb = FixtureBuilder()
    g = fb.append([coinbase_tx(1, [(1_000_000, alice.public_bytes)])])
    assert node.submit_raw_block(encode_block(g), 1).accepted
    tx = transfer_tx(
        [(Outpoint(g.transactions[0].txid, 0), alice.public_bytes)],
        [(400_000, bob.public_bytes), (599_000, alice.public_bytes)],
        {0: alice},
    )
    b2 = fb.append([coinbase_tx(2, [(1_001_000, alice.public_bytes)]), tx])
    assert node.submit_raw_block(encode_block(b2), 2).accepted
    source_tip = store.tip_hash
    source_snapshot = store.snapshot_utxos()
    store.close()

    target = str(tmp_path / "rebuilt.db")
    report = replay_chain(
        src_path, str(tmp_path / "logs"), keep_target=target
    )
    assert report.failed == 0
    rebuilt = SqliteStore(target)
    assert rebuilt.tip_hash == source_tip
    assert rebuilt.snapshot_utxos() == source_snapshot
    rebuilt.close()


def test_runlog_records_run_id_and_unchanged_snapshots(tmp_path):
    logdir = tmp_path / "logs"
    logger = RunLogger(str(logdir), run_id="run-testfixed01")
    store = SqliteStore(":memory:")
    node = LedgerNode(store, logger)
    alice = named_key("alice")
    fb = FixtureBuilder()
    g = fb.append([coinbase_tx(1, [(1_000_000, alice.public_bytes)])])
    node.submit_raw_block(encode_block(g), 1)
    # A structurally valid but semantically bad block (wrong height).
    from utxo_ledger.encoding import Block

    bad = Block(1, 5, store.tip_hash, (coinbase_tx(5, [(1, alice.public_bytes)]),))
    res = node.submit_raw_block(encode_block(bad), 2)
    assert not res.accepted
    summary = {"passed": 1, "failed": 1}
    summary_path = logger.close(summary)

    log_path = os.path.join(str(logdir), "run-testfixed01.jsonl")
    records = [json.loads(line) for line in open(log_path, encoding="utf-8")]
    assert all(r["run_id"] == "run-testfixed01" for r in records)
    rejects = [r for r in records if r.get("event") == "block_result" and r["verdict"] == "rejected"]
    assert rejects and rejects[0]["category"] == "state"
    assert rejects[0]["snapshot_before"] == rejects[0]["snapshot_after"]
    assert os.path.exists(summary_path)
