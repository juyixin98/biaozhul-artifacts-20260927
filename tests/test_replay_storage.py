"""SQLite index store and offline replay (idempotency, ordering)."""

import pytest

from basefee_model.fixtures import canonical_scenario, wallet, sign_eip1559_tx
from basefee_model.replay.replay import Replayer, payload_from_dict
from basefee_model.errors import FailureCode
from basefee_model.storage.store import IndexStore


def _payloads(scenario):
    return [payload_from_dict(b) for b in scenario["blocks"]]


def test_replay_canonical_and_persist(store, silent_log):
    sc = canonical_scenario()
    rep = Replayer(store, sc["genesis_base_fee"], sc["gas_limit"], sc["alloc"],
                   log=silent_log)
    r = rep.run(_payloads(sc), request_id="t1")
    assert r.ok()
    assert r.applied == [1, 2, 3, 4, 5, 6, 7]
    assert store.latest_number() == 7
    block1 = store.get_block(1)
    assert block1["base_fee"] == sc["genesis_base_fee"]
    # money columns decode back to int
    assert isinstance(block1["burned"], int)


def test_replay_is_idempotent(store, silent_log):
    sc = canonical_scenario()
    rep = Replayer(store, sc["genesis_base_fee"], sc["gas_limit"], sc["alloc"],
                   log=silent_log)
    r1 = rep.run(_payloads(sc), request_id="t1")
    r2 = rep.run(_payloads(sc), request_id="t2")
    assert r1.applied and not r2.applied
    assert r2.skipped == [1, 2, 3, 4, 5, 6, 7]
    # Totals are not double-counted.
    assert store.totals()["conserved"] is True


def test_replay_stops_on_gap(store, silent_log, funder):
    sc = canonical_scenario()
    payloads = _payloads(sc)
    # Remove block 3 -> block 4 must be rejected as out of order.
    payloads = [p for p in payloads if p.number != 3]
    rep = Replayer(store, sc["genesis_base_fee"], sc["gas_limit"], sc["alloc"],
                   log=silent_log)
    r = rep.run(payloads, request_id="gap")
    assert not r.ok()
    assert r.failures[0]["code"] == "bad_block_number"
    assert r.applied == [1, 2]


def test_replay_invalid_tx_is_categorized(store, silent_log):
    # Block 1 carries an unsigned-ish invalid cap: build a tx whose fee cap is
    # below the genesis base fee.
    w = wallet("broke")
    alloc = {w.address_hex: 10 ** 30}
    _, raw = sign_eip1559_tx(w, 0, max_fee_per_gas=1,
                             max_priority_fee_per_gas=1, gas_limit=21_000)
    from basefee_model.replay.replay import BlockPayload
    rep = Replayer(store, 1_000_000_000, 30_000_000, alloc,
                   log=silent_log)
    r = rep.run([BlockPayload(number=1, raw_transactions=[raw],
                              tx_gas_used=[21_000])], request_id="badcap")
    assert not r.ok()
    assert r.failures[0]["code"] == FailureCode.FEE_CAP_BELOW_BASE_FEE.value
    # Nothing persisted for the failed block.
    assert store.latest_number() is None


def test_store_sender_index(store, silent_log):
    sc = canonical_scenario()
    rep = Replayer(store, sc["genesis_base_fee"], sc["gas_limit"], sc["alloc"],
                   log=silent_log)
    rep.run(_payloads(sc), request_id="idx")
    funder = wallet("funder")
    txs = store.get_transactions_by_sender(funder.address_hex)
    # blocks 1,2,3,6,7 carry the funder tx (4,5 empty)
    assert [t["block_number"] for t in txs] == [1, 2, 3, 6, 7]
    assert store.totals()["conserved"] is True
