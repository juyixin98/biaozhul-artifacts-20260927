"""Storage-layer tests: revocable index, atomic switch and durable resume."""
from __future__ import annotations

import threading

import pytest

from tests.conftest import load_recording
from reorgindex.storage.store import IndexStore, SwitchInterrupted

pytestmark = pytest.mark.storage

REC = load_recording("interrupt")
BLOCKS = {b["name"]: b["block"] for b in REC["blocks"]}
HASHES = {b["name"]: b["block"] for b in REC["blocks"]}  # payloads, hashes derived below

from reorgindex.crypto.hashing import block_identity_hash  # noqa: E402

H = {name: block_identity_hash(b) for name, b in HASHES.items()}


def _deltas(name: str):
    from reorgindex.kernel.derivation import block_ledger_deltas
    return block_ledger_deltas(BLOCKS[name])


@pytest.fixture
def store(tmp_path):
    s = IndexStore(tmp_path / "store.db")
    yield s
    s.close()


def _seed_genesis_and_mains(store: IndexStore) -> None:
    for name in ("g0", "m1", "m2"):
        b = BLOCKS[name]
        store.insert_block(
            block_hash=H[name], block=b, weight=b["difficulty"],
            is_active=(name in ("g0", "m1", "m2")),
        )
        if name == "g0":
            store.apply_extension(block_hash=H[name], height=0, deltas=_deltas(name), weight=b["difficulty"])
        else:
            store.apply_extension(block_hash=H[name], height=b["height"], deltas=_deltas(name), weight=b["difficulty"])


def test_extension_materializes_accounts(store):
    _seed_genesis_and_mains(store)
    addrs = REC["addresses"]
    # g0: alice 1000 bob 500; m1/m2: alice pays bob 10 twice
    assert store.account_balance(addrs["alice"]) == 980
    assert store.account_balance(addrs["bob"]) == 520
    assert store.account_nonce(addrs["alice"]) == 2
    assert store.derived_event_count() > 0


def test_switch_detach_then_attach_is_atomic_per_phase(store):
    _seed_genesis_and_mains(store)
    addrs = REC["addresses"]

    # Store fork blocks first (inert).
    for name in ("f1", "f2"):
        b = BLOCKS[name]
        store.insert_block(block_hash=H[name], block=b, weight=b["difficulty"], is_active=False)

    # Detach m1,m2 only -> the committed intermediate state must be internally
    # consistent (genesis view), never a mix.
    store.begin_detach(
        new_tip=H["f1"], detach=[H["m1"], H["m2"]],
        attach=[H["f1"]], request_id="req-test",
    )
    plan = store.get_plan()
    assert plan["phase"] == "DETACHED"
    assert store.active_hashes() == [H["g0"]]
    assert store.account_balance(addrs["alice"]) == 1_000
    assert store.account_balance(addrs["bob"]) == 500
    # No events from m1/m2 survive.
    assert store.derived_event_count() == len(_deltas("g0"))

    # Finish attach.
    store.finish_attach(
        attach=[{"hash": H["f1"], "height": 1, "weight": 16}],
        deltas_by_hash={H["f1"]: _deltas("f1")},
    )
    assert store.get_plan() is None
    assert store.active_hashes() == [H["g0"], H["f1"]]
    assert store.account_balance(addrs["alice"]) == 900
    assert store.account_balance(addrs["carol"]) == 100


def test_crash_after_detach_is_recoverable_from_new_connection(tmp_path):
    db = tmp_path / "crash.db"
    store = IndexStore(db)
    _seed_genesis_and_mains(store)
    for name in ("f1", "f2"):
        b = BLOCKS[name]
        store.insert_block(block_hash=H[name], block=b, weight=b["difficulty"], is_active=False)

    with pytest.raises(SwitchInterrupted):
        store.apply_switch(
            detach=[H["m1"], H["m2"]],
            attach=[{"hash": H["f1"], "height": 1, "weight": 16}],
            deltas_by_hash={H["f1"]: _deltas("f1")},
            request_id="req-crash",
            new_tip=H["f1"],
            crash_point="after_detach",
        )
    store.close()

    # Simulate process restart: a brand-new connection sees the persisted plan
    # and completes it exactly once.
    store2 = IndexStore(db)
    plan = store2.get_plan()
    assert plan is not None and plan["phase"] == "DETACHED"
    resumed = store2.resume_switch({H["f1"]: _deltas("f1")})
    assert resumed["new_tip"] == H["f1"]
    assert store2.get_plan() is None
    assert store2.active_hashes() == [H["g0"], H["f1"]]
    addrs = REC["addresses"]
    assert store2.account_balance(addrs["carol"]) == 100
    # Resuming again is a no-op.
    assert store2.resume_switch({}) is None
    store2.close()


def test_reader_never_sees_half_written_attach(store):
    _seed_genesis_and_mains(store)
    for name in ("f1", "f2"):
        b = BLOCKS[name]
        store.insert_block(block_hash=H[name], block=b, weight=b["difficulty"], is_active=False)

    store.begin_detach(
        new_tip=H["f1"], detach=[H["m1"], H["m2"]], attach=[H["f1"]],
        request_id="req-lock",
    )
    barrier = threading.Barrier(2)

    def reader_observations(out: list):
        barrier.wait()
        # Sample repeatedly during attach; every read must be a coherent view.
        for _ in range(50):
            hashes = tuple(store.active_hashes())
            assert hashes in ((H["g0"],), (H["g0"], H["f1"])), hashes
            out.append(hashes)

    observed: list[tuple] = []
    t = threading.Thread(target=reader_observations, args=(observed,))
    t.start()
    barrier.wait()
    store.finish_attach(
        attach=[{"hash": H["f1"], "height": 1, "weight": 16}],
        deltas_by_hash={H["f1"]: _deltas("f1")},
    )
    t.join()
    assert (H["g0"], H["f1"]) in observed


def test_tx_locations_track_active_flag(store):
    _seed_genesis_and_mains(store)
    m1_txid = BLOCKS["m1"]["transactions"][0]["txid"]
    # A fork occurrence of a different txid needs its block row first (FK).
    f1 = BLOCKS["f1"]
    store.insert_block(block_hash=H["f1"], block=f1, weight=f1["difficulty"], is_active=False)
    f1_txid = f1["transactions"][0]["txid"]
    store.insert_fork_locations(H["f1"], 1, [f1_txid])

    m1_rows = store.tx_contributions(m1_txid)
    assert all(r["on_active"] for r in m1_rows if r["block_hash"] == H["m1"])
    f1_rows = store.tx_contributions(f1_txid)
    assert len(f1_rows) == 1 and f1_rows[0]["on_active"] == 0
