"""Tests for SQLite persistence and the state service (signed journal)."""
from __future__ import annotations

import pytest

from smt.crypto import empty_at
from smt.services import StateService
from smt.storage import SqliteNodeStore

HMAC = "unit-test-key"


@pytest.fixture()
def persisted(tmp_path):
    path = str(tmp_path / "state.db")
    store = SqliteNodeStore(path)
    svc = StateService(store, hmac_key=HMAC)
    yield store, svc
    store.close()


KA = "00" * 30 + "ab" + "00"
KB = "00" * 30 + "ab" + "01"
KC = "ff" * 32


def test_genesis_records_empty_root(persisted):
    _, svc = persisted
    assert svc.root == empty_at(0)
    assert svc.revision >= 1
    revs = svc.store.list_revisions()
    assert revs[-1]["root"] == empty_at(0).hex()


def test_set_get_delete_cycle(persisted):
    _, svc = persisted
    svc.update_one(KA, "alpha")
    exists, value = svc.get_value(KA)
    assert exists and value == b"alpha"
    assert svc.get_value(KA)[0] is True

    svc.update_one(KA, None)
    assert svc.get_value(KA) == (False, None)


def test_empty_string_value_is_stored_not_deleted(persisted):
    _, svc = persisted
    svc.update_one(KA, "")
    assert svc.get_value(KA) == (True, b"")
    rows = svc.store.journal_rows()
    assert rows[0]["kind"] == "set" and rows[0]["value_hex"] == ""


def test_batch_duplicate_rejected_atomically(persisted):
    _, svc = persisted
    with pytest.raises(Exception) as exc:
        svc.apply_batch([(KA, "a"), (KA, "b")])
    assert exc.value.category == "duplicate_key"
    # nothing applied: still genesis empty
    assert svc.root == empty_at(0)
    assert svc.store.journal_rows() == []


def test_batch_equivalence_through_service(persisted):
    _, svc = persisted
    effects = svc.apply_batch([(KC, "gamma"), (KA, "alpha"), (KB, "beta")])
    batch_root = svc.root

    # second service, same updates one by one in ascending key order
    import os
    path2 = os.path.join(os.path.dirname(svc.store.path), "other.db")
    store2 = SqliteNodeStore(path2)
    svc2 = StateService(store2, hmac_key=HMAC)
    for k in sorted([KA, KB, KC]):
        svc2.update_one(k, {"00" * 30 + "ab" + "00": "alpha",
                            "00" * 30 + "ab" + "01": "beta",
                            "ff" * 32: "gamma"}[k])
    assert svc2.root == batch_root
    assert len(effects) == 3
    store2.close()


def test_journal_rows_are_signed_and_chain_roots(persisted):
    _, svc = persisted
    svc.update_one(KA, "alpha")
    svc.update_one(KB, "beta")
    rows = svc.store.journal_rows()
    assert [r["seq"] for r in rows] == [1, 2]
    assert rows[0]["new_root"] == rows[1]["prev_root"]
    # signatures verify with the configured key and fail with another key
    from smt.crypto import verify_payload
    for r in rows:
        assert verify_payload(r["payload"], r["signature_hex"], HMAC)
        assert not verify_payload(r["payload"], r["signature_hex"], "wrong-key")


def test_delete_of_absent_key_writes_no_journal(persisted):
    _, svc = persisted
    effects = svc.apply_batch([(KA, None)])
    assert effects == []
    assert svc.store.journal_rows() == []
    assert svc.root == empty_at(0)


def test_old_root_still_verifiable_after_later_updates(persisted):
    _, svc = persisted
    svc.update_one(KA, "alpha")
    r1 = svc.root
    svc.update_one(KB, "beta")
    r2 = svc.root
    assert r1 != r2

    # issue membership proof against the OLD root r1
    old_proof = svc.issue_proof(KA, root=r1)
    assert old_proof["root"] == r1.hex()
    res = svc.check_proof(old_proof)
    assert res.ok

    # kb did not exist at r1: its historical proof is non-membership
    old_kb = svc.issue_proof(KB, root=r1)
    assert old_kb["exists"] is False
    assert svc.check_proof(old_kb).ok

    # presenting r1's proof under r2 must fail
    import copy
    swapped = copy.deepcopy(old_proof)
    swapped["root"] = r2.hex()
    assert svc.check_proof(swapped).verdict.value == "root_mismatch"


def test_service_reopens_with_latest_root(persisted):
    store, svc = persisted
    svc.update_one(KA, "alpha")
    r = svc.root
    store.close()

    store2 = SqliteNodeStore(store.path)
    svc2 = StateService(store2, hmac_key=HMAC)
    assert svc2.root == r
    assert svc2.get_value(KA) == (True, b"alpha")
    store2.close()


def test_concurrent_writers_and_readers(persisted):
    import threading

    _, svc = persisted
    svc.update_one(KA, "alpha")
    errors: list[Exception] = []

    def writer(i):
        try:
            key = f"{i % 8:064x}"
            svc.update_one(key, f"value-{i}")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def reader():
        try:
            for _ in range(20):
                svc.get_value(KA)
                svc.issue_proof(KA)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(16)]
    threads += [threading.Thread(target=reader) for _ in range(4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert errors == []
    # final state is internally consistent: current proof verifies
    proof = svc.issue_proof(KA)
    assert svc.check_proof(proof).ok


def test_oversized_value_rejected(persisted):
    _, svc = persisted
    big = "x" * (0xFFFF + 1)
    with pytest.raises(Exception) as exc:
        svc.update_one(KA, big)
    assert exc.value.category == "value_too_large"
    assert svc.root == empty_at(0)
    assert svc.store.journal_rows() == []


def test_proof_binds_root_key_depth(persisted):
    _, svc = persisted
    svc.apply_batch([(KA, "alpha"), (KB, "beta")])
    p = svc.issue_proof(KA)
    assert p["root"] == svc.root.hex()
    assert p["key"] == KA
    assert p["terminal_depth"] == 256
