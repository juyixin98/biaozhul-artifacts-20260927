"""Tests for offline journal replay: signature, chaining and tamper classes."""
from __future__ import annotations

import copy
import json

import pytest

from smt.crypto import empty_at
from smt.kernel import MemoryNodeStore
from smt.services import (
    ReplayError,
    StateService,
    load_journal_file,
    replay_records,
)
from smt.storage import SqliteNodeStore

KEY = "unit-test-key"
KA = "00" * 30 + "ab" + "00"
KB = "ff" * 32


@pytest.fixture()
def journal(tmp_path):
    store = SqliteNodeStore(str(tmp_path / "j.db"))
    svc = StateService(store, hmac_key=KEY)
    svc.update_one(KA, "alpha")
    svc.update_one(KB, "gamma")
    svc.update_one(KA, None)
    svc.update_one(KA, "alpha2")
    rows = store.journal_rows()
    root = svc.root
    path = tmp_path / "journal.json"
    path.write_text(json.dumps({"version": "smt-v1", "records": rows}))
    store.close()
    return rows, root, path


def test_replay_rebuilds_final_root(journal):
    rows, root, _ = journal
    target = MemoryNodeStore()
    outcome = replay_records(copy.deepcopy(rows), target, KEY)
    assert outcome.root_matches
    assert outcome.final_root == root
    assert outcome.applied == 4
    assert outcome.first_seq == 1 and outcome.last_seq == 4


def test_replay_from_empty_matches_empty_root():
    outcome = replay_records([], MemoryNodeStore(), KEY)
    assert outcome.applied == 0
    assert outcome.final_root == empty_at(0)
    assert outcome.root_matches


def test_replay_rejects_bad_signature(journal):
    rows, _, _ = journal
    rows[1]["signature_hex"] = "00" * 32
    with pytest.raises(ReplayError) as exc:
        replay_records(copy.deepcopy(rows), MemoryNodeStore(), KEY)
    assert exc.value.category == "bad_signature"
    assert exc.value.seq == 2
    # diagnostics identify the offending record without dumping the key
    assert exc.value.state["key_hex"].endswith("…")


def test_replay_rejects_wrong_key(journal):
    rows, _, _ = journal
    with pytest.raises(ReplayError) as exc:
        replay_records(copy.deepcopy(rows), MemoryNodeStore(), "a-different-key")
    assert exc.value.category == "bad_signature"
    assert exc.value.seq == 1


def test_replay_rejects_tampered_value(journal):
    rows, _, _ = journal
    rows[0]["payload"]["value_hex"] = (b"alpha" + b"!").hex()
    with pytest.raises(ReplayError) as exc:
        replay_records(copy.deepcopy(rows), MemoryNodeStore(), KEY)
    assert exc.value.category == "bad_signature"


def test_replay_rejects_reordered_chain(journal):
    rows, _, _ = journal
    rows[0], rows[1] = rows[1], rows[0]
    with pytest.raises(ReplayError) as exc:
        replay_records(copy.deepcopy(rows), MemoryNodeStore(), KEY)
    assert exc.value.category in ("sequence_gap", "chain_break", "bad_signature")


def test_replay_rejects_missing_record(journal):
    rows, _, _ = journal
    del rows[1]
    with pytest.raises(ReplayError) as exc:
        replay_records(copy.deepcopy(rows), MemoryNodeStore(), KEY)
    assert exc.value.category == "sequence_gap"


def test_replay_rejects_root_fork(journal):
    # sign-valid payload but prev_root no longer chains (simulate by rewriting
    # the signed prev_root; HMAC must catch it first -> bad_signature)
    rows, _, _ = journal
    rows[1]["payload"]["prev_root"] = "ab" * 32
    with pytest.raises(ReplayError) as exc:
        replay_records(copy.deepcopy(rows), MemoryNodeStore(), KEY)
    assert exc.value.category == "bad_signature"


def test_load_journal_file_roundtrip(journal, tmp_path):
    _, _, path = journal
    records = load_journal_file(str(path))
    assert len(records) == 4
    # exported form carries payload_json; replayer must accept it
    outcome = replay_records(records, MemoryNodeStore(), KEY)
    assert outcome.root_matches
