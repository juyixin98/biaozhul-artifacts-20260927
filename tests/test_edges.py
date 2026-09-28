"""Focused kernel edge tests: tie-break, query consistency, finality boundary."""
from __future__ import annotations

import pytest

from reorgindex.replay.builder import BranchBuilder, FixtureKeys
from tests.oracle.reference import load_blocks


@pytest.mark.kernel
def test_equal_weight_tie_breaks_to_lower_tip_hash(make_app):
    """Two equal-weight children of g0: the lower tip hash wins deterministically."""
    keys = FixtureKeys.create()
    main = BranchBuilder(keys, difficulty=4)
    main.genesis(name="g0")
    main.child(name="a", transfers=[{"sender": "alice", "recipient": "bob", "amount": 1}])
    fork = main.snapshot(tip_name="g0")
    fork.nonces.clear()
    fork.child(name="b", transfers=[{"sender": "alice", "recipient": "carol", "amount": 1}])

    recording = {
        "finality_depth": 3,
        "producer_address": keys.addresses["producer"],
        "blocks": [{"name": n, "block": main.blocks[n]} for n in ("g0", "a", "b")],
    }
    app = make_app(recording, name="tie.db")
    app.engine.ingest(main.blocks["g0"], request_id="t-g")
    r_a = app.engine.ingest(main.blocks["a"], request_id="t-a")
    assert r_a.outcome == "ACCEPT_EXTEND"
    r_b = app.engine.ingest(main.blocks["b"], request_id="t-b")

    from reorgindex.crypto.hashing import block_identity_hash

    ha = block_identity_hash(main.blocks["a"])
    hb = block_identity_hash(main.blocks["b"])
    if hb < ha:
        assert r_b.outcome == "ACCEPT_SWITCH"
        assert app.store.active_tip()["hash"] == hb
    else:
        assert r_b.outcome == "ACCEPT_FORK"
        assert app.store.active_tip()["hash"] == ha

    # Either way the decision is deterministic: re-running yields same tip.
    app2 = make_app(recording, name="tie2.db")
    for n in ("g0", "a", "b"):
        app2.engine.ingest(main.blocks[n], request_id=f"t2-{n}")
    assert app2.store.active_tip()["hash"] == app.store.active_tip()["hash"]


@pytest.mark.kernel
def test_stored_fork_is_never_visible_in_queries(make_app, deep_fork_docs):
    recording, _ = deep_fork_docs
    app = make_app(recording, name="invisible.db")
    by_name = {b["name"]: b["block"] for b in recording["blocks"]}
    for name in ("g0", "m1", "m2", "m3", "m4", "m5", "d1"):
        app.engine.ingest(by_name[name], request_id=f"q-{name}")
    # d1 is a non-active fork: its carol credit must not be queryable.
    carol = recording["addresses"]["carol"]
    # main chain at m5: carol only received m4 (5) + m5 (10) = 15
    assert app.store.account_balance(carol) == 15
    d1_txid = by_name["d1"]["transactions"][0]["txid"]
    assert app.store.active_tx_exists(d1_txid) is False
    blocks = load_blocks(recording)
    # confirmations() of an inactive fork block is None
    assert app.engine.confirmations(blocks["d1"].hash) is None
    assert app.engine.is_final(blocks["d1"].hash) is False


@pytest.mark.kernel
def test_unknown_parent_suspends_and_height_mismatch_rejects(make_app, short_fork_docs):
    recording, _ = short_fork_docs
    app = make_app(recording, name="unk.db")
    by_name = {b["name"]: b["block"] for b in recording["blocks"]}
    app.engine.ingest(by_name["g0"], request_id="u-g")

    # m2 fed before m1: unknown parent -> suspended, not rejected.
    r = app.engine.ingest(by_name["m2"], request_id="u-m2-early")
    assert r.outcome == "PENDING"
    assert app.store.pending_count() == 1

    # A statelessly-valid sealed block (real PoW + producer signature) whose
    # height disagrees with its known parent is rejected HEIGHT_MISMATCH.
    from reorgindex.replay.builder import mine

    from reorgindex.replay.builder import FixtureKeys as FK
    # Rebuild a minimal g0 + mismatching-height block in a fresh key set so we
    # control the mining; simpler: mine using the existing fixture producer key
    # is not possible (keys aren't serialized), so use a fresh chain.
    keys2 = FK.create()
    b2 = BranchBuilder(keys2, difficulty=4)
    b2.genesis(name="g0")
    tx = b2.make_transfer(sender="alice", recipient="bob", amount=1, nonce=1)
    bogus, _ = mine(
        height=5,  # claims height 5 while parent g0 is height 0
        parent=b2.hashes["g0"],
        producer=keys2.producer,
        transactions=[tx],
        difficulty=4,
        timestamp="2026-09-27T00:05:00+00:00",
    )
    app3 = make_app(
        {"finality_depth": 3, "producer_address": keys2.addresses["producer"]},
        name="mismatch.db",
    )
    app3.engine.ingest(b2.blocks["g0"], request_id="mm-g")
    from reorgindex.kernel.errors import IngestionError, RejectReason

    with pytest.raises(IngestionError) as exc:
        app3.engine.ingest(bogus, request_id="mm-bad")
    assert exc.value.reason is RejectReason.HEIGHT_MISMATCH


@pytest.mark.kernel
def test_invalid_orphan_at_release_is_isolated_and_archived(make_app):
    """Two orphans sharing a parent: one valid, one statefully invalid.

    The invalid one must not abort the releasing request; it is archived
    inactive with a concrete failure category, and the valid one extends.
    """
    keys = FixtureKeys.create()
    b = BranchBuilder(keys, difficulty=4)
    g0, g0_hash = b.genesis(name="g0")

    from reorgindex.replay.builder import mine

    ok_tx = b.make_transfer(sender="alice", recipient="bob", amount=10, nonce=1)
    bad_tx = b.make_transfer(sender="alice", recipient="bob", amount=10_000, nonce=1)
    ok_block, ok_hash = mine(
        height=1, parent=g0_hash, producer=keys.producer, transactions=[ok_tx],
        difficulty=4, timestamp="2026-09-27T00:01:00+00:00",
    )
    bad_block, bad_hash = mine(
        height=1, parent=g0_hash, producer=keys.producer, transactions=[bad_tx],
        difficulty=4, timestamp="2026-09-27T00:02:00+00:00",
    )
    recording = {"finality_depth": 3, "producer_address": keys.addresses["producer"]}
    app = make_app(recording, name="iso.db")

    # Children before their parent -> both suspended.
    r_bad = app.engine.ingest(bad_block, request_id="iso-bad")
    r_ok = app.engine.ingest(ok_block, request_id="iso-ok")
    assert r_bad.outcome == "PENDING" and r_ok.outcome == "PENDING"
    assert app.store.pending_count() == 2

    # Genesis releases the cascade; the invalid orphan is isolated.
    r_g = app.engine.ingest(g0, request_id="iso-g0")
    rejected = {x["block_hash"]: x["reason"] for x in r_g.rejected_orphans}
    assert ok_hash in set(r_g.released)
    assert rejected == {bad_hash: "INSUFFICIENT_FUNDS"}
    assert app.store.active_tip()["hash"] == ok_hash
    assert app.store.is_active_hash(bad_hash) is False
    assert app.store.has_block(bad_hash) is True
    assert app.store.pending_count() == 0

    diag = [
        d for d in app.store.diagnostics(limit=50)
        if d["block_hash"] == bad_hash and d["reason"] == "INSUFFICIENT_FUNDS"
    ]
    assert diag and diag[0]["request_id"] == "iso-g0"


@pytest.mark.kernel
def test_finality_boundary_exactly_k_confirmations(make_app):
    """At depth K confirmations exactly, reorg is allowed; K+1 is final."""
    keys = FixtureKeys.create()
    main = BranchBuilder(keys, difficulty=4)
    main.genesis(name="g0")
    for i in range(1, 5):  # m1..m4
        main.child(
            name=f"m{i}",
            transfers=[{"sender": "alice", "recipient": "bob", "amount": 1}],
        )
    # Weighted fork from g0: 4+16 = 20 vs 5*4 = 20 tie at m4 arrival;
    # make the fork strictly heavier with a second weighted block is not
    # needed here: at tip m3 (3*4=12) fork 4+16=20 wins and detaches blocks
    # with at most 3 confirmations (<= K=3).
    fork = main.snapshot(tip_name="g0")
    fork.nonces.clear()
    fork.child(
        name="w1", difficulty=16,
        transfers=[{"sender": "alice", "recipient": "carol", "amount": 1}],
    )
    recording = {
        "finality_depth": 3,
        "producer_address": keys.addresses["producer"],
        "blocks": [{"name": n, "block": main.blocks[n]} for n in
                   ("g0", "m1", "m2", "m3", "m4", "w1")],
    }
    app = make_app(recording, name="boundary2.db")
    for name in ("g0", "m1", "m2", "m3"):
        app.engine.ingest(main.blocks[name], request_id=f"k-{name}")
    r = app.engine.ingest(main.blocks["w1"], request_id="k-w1")
    assert r.outcome == "ACCEPT_SWITCH"
    assert r.switch["rollback_height_range"] == [1, 3]
