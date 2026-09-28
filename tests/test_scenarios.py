"""End-to-end scenario tests against the committed JSON fixtures.

Every scenario is compared with the independent oracle in
``tests/oracle/reference.py``: the best chain, its hashes, cumulative weight
and the *complete* per-account projection are recomputed from raw fixture
blocks and must match the engine's derived index exactly.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from reorgindex.storage.store import SwitchInterrupted  # noqa: E402
from tests.oracle.reference import (  # noqa: E402
    expected_projection,
    load_blocks,
    simulate_arrivals,
)

pytestmark = pytest.mark.scenario


def replay(app, recording, *, inject_crash: bool = True):
    by_name = {b["name"]: b["block"] for b in recording["blocks"]}
    decisions: dict[str, dict] = {}
    crashed = False
    for name in recording["arrival_order"]:
        crash = "after_detach" if inject_crash and name == recording.get("crash_after") else None
        try:
            result = app.engine.ingest(
                by_name[name], request_id=f"req-{name}", crash_point=crash
            )
            decisions[name] = result.as_dict()
        except SwitchInterrupted:
            crashed = True
            decisions[name] = {"outcome": "SWITCH_INTERRUPTED"}
            app.engine.resume_switch(request_id=f"req-{name}-resume")
    return decisions, crashed


def assert_projection_matches_oracle(app, recording, tip_name: str):
    expected = expected_projection(recording, tip_name=tip_name)
    assert app.store.active_hashes() == expected["chain_hashes"]
    assert app.engine.active_cumulative_weight() == expected["cumulative_weight"]
    for address, balance in expected["balances"].items():
        assert app.store.account_balance(address) == balance, address
    for address, nonce in expected["nonces"].items():
        assert app.store.account_nonce(address) == nonce, address
    snapshot = app.store.accounts_snapshot()
    assert set(snapshot) == set(expected["balances"])
    for tx_id in expected["txids"]:
        rows = [r for r in app.store.tx_contributions(tx_id) if r["on_active"]]
        assert len(rows) == 1, tx_id
    return expected


def assert_outcomes_match_oracle(recording):
    return simulate_arrivals(
        recording, finality_depth=int(recording["finality_depth"])
    )


# ---------------------------------------------------------------------
def test_short_fork_wins_with_rollback_range(make_app, short_fork_docs):
    recording, expected_doc = short_fork_docs
    app = make_app(recording, name="short.db")
    decisions, crashed = replay(app, recording, inject_crash=False)
    assert not crashed

    # Concrete outcomes (not just "endpoint callable"):
    assert decisions["g0"]["outcome"] == "ACCEPT_EXTEND"
    assert decisions["m1"]["outcome"] == "ACCEPT_EXTEND"
    assert decisions["m2"]["outcome"] == "ACCEPT_EXTEND"
    assert decisions["m3"]["outcome"] == "ACCEPT_EXTEND"
    assert decisions["o1"]["outcome"] == "PENDING"
    assert decisions["f1"]["outcome"] == "ACCEPT_SWITCH"
    assert decisions["f2"]["outcome"] == "ACCEPT_EXTEND"

    # Rollback interval reported on the winning switch:
    switch = decisions["f1"]["switch"]
    assert switch["rollback_height_range"] == [1, 3]
    assert len(switch["detached"]) == 3 and len(switch["attached"]) == 1

    # Orphan released: f2 extends f1 and must release o1.
    blocks = load_blocks(recording)
    assert decisions["f2"]["released"] == [blocks["o1"].hash]

    assert_projection_matches_oracle(app, recording, "o1")

    # Duplicate txid exists on both branches but contributes exactly once.
    dup = expected_doc["duplicate_txid"]
    rows = app.store.tx_contributions(dup)
    assert len(rows) == 2
    active_rows = [r for r in rows if r["on_active"]]
    assert len(active_rows) == 1
    blocks = load_blocks(recording)
    assert active_rows[0]["block_hash"] == blocks["f1"].hash

    # Final balances asserted against the fixture's explicit expected section.
    for address, balance in expected_doc["final_balances"].items():
        assert app.store.account_balance(address) == balance

    # Every recorded decision carries a request id and key state.
    diag_rows = app.store.diagnostics(limit=50)
    assert len(diag_rows) >= 7
    for row in diag_rows:
        assert row["request_id"].startswith("req-")


def test_short_fork_matches_independent_oracle_simulation(make_app, short_fork_docs):
    recording, _ = short_fork_docs
    app = make_app(recording, name="short2.db")
    decisions, _ = replay(app, recording, inject_crash=False)
    oracle = assert_outcomes_match_oracle(recording)
    for name in ("g0", "m1", "m2", "m3", "f1", "f2"):
        assert decisions[name]["outcome"] == oracle[name].outcome, name
    # o1 is suspended on arrival (engine records PENDING) and released when
    # f2 makes its parent the active tip; the oracle's final disposition for
    # o1 is the released ACCEPT_EXTEND with o1 as tip.
    assert decisions["o1"]["outcome"] == "PENDING"
    assert decisions["f2"]["released"]
    assert oracle["o1"].outcome == "ACCEPT_EXTEND"
    assert oracle["o1"].active_tip_after == "o1"
    blocks = load_blocks(recording)
    assert app.store.active_tip()["hash"] == blocks["o1"].hash


def test_deep_fork_is_rejected_with_finality_category(make_app, deep_fork_docs):
    recording, expected_doc = deep_fork_docs
    app = make_app(recording, name="deep.db")
    decisions, crashed = replay(app, recording, inject_crash=False)
    assert not crashed

    d2 = decisions["d2"]
    assert d2["outcome"] == "REJECTED"
    assert d2["reason"] == "REORG_FINALIZED"
    assert d2["switch"]["rollback_heights"] == [1, 5]

    # The weaker d1 was stored as a non-active fork.
    assert decisions["d1"]["outcome"] == "ACCEPT_FORK"

    # Active chain is unchanged: m5 remains tip; fork balances never visible.
    blocks = load_blocks(recording)
    assert app.store.active_tip()["hash"] == blocks["m5"].hash
    assert_projection_matches_oracle(app, recording, "m5")
    for address, balance in expected_doc["final_balances"].items():
        assert app.store.account_balance(address) == balance

    # Confirmation/finality query surface.
    assert app.engine.confirmations(blocks["m1"].hash) == 5
    assert app.engine.is_final(blocks["m1"].hash) is True
    assert app.engine.is_final(blocks["m3"].hash) is False

    # Fork block transactions are recorded but inactive.
    d1_txid = recording["blocks"]
    d1_tx = next(b["block"] for b in d1_txid if b["name"] == "d1")["transactions"][0]
    rows = app.store.tx_contributions(d1_tx["txid"])
    assert rows and all(not r["on_active"] for r in rows)

    # Diagnostics carry the rejection reason.
    found = [r for r in app.store.diagnostics(limit=100) if r["reason"] == "REORG_FINALIZED"]
    assert found and found[0]["block_hash"] == blocks["d2"].hash


def test_shallow_switch_just_inside_finality_boundary_is_allowed(make_app, deep_fork_docs):
    """K=3: detaching a block with exactly 3 confirmations must be allowed."""
    recording, _ = deep_fork_docs
    app = make_app(recording, name="boundary.db")
    by_name = {b["name"]: b["block"] for b in recording["blocks"]}
    # Feed only g0..m3 (tip height 3), then the weighted d1: it replaces m1..m3
    # whose shallowest block m1 has 3 confirmations (3 <= K) -> allowed.
    for name in ("g0", "m1", "m2", "m3", "d1"):
        result = app.engine.ingest(by_name[name], request_id=f"req-b-{name}")
    assert result.outcome == "ACCEPT_SWITCH"
    assert result.switch["rollback_height_range"] == [1, 3]
    blocks = load_blocks(recording)
    assert app.store.active_tip()["hash"] == blocks["d1"].hash


def test_switch_interrupted_then_resumed(make_app, interrupt_docs):
    recording, expected_doc = interrupt_docs
    app = make_app(recording, name="interrupt.db")
    decisions, crashed = replay(app, recording, inject_crash=True)
    assert crashed is True
    assert decisions["f1"]["outcome"] == "SWITCH_INTERRUPTED"
    assert decisions["f2"]["outcome"] == "ACCEPT_EXTEND"

    # Plan persisted at DETACHED during the interruption, then consumed.
    assert app.store.get_plan() is None
    assert_projection_matches_oracle(app, recording, "f2")
    for address, balance in expected_doc["final_balances"].items():
        assert app.store.account_balance(address) == balance

    # Restart on a fresh engine/connection with the same DB: no plan, and the
    # projection is intact.
    app2 = make_app(recording, name="interrupt.db")
    blocks = load_blocks(recording)
    assert app2.store.active_hashes() == [blocks["g0"].hash, blocks["f1"].hash, blocks["f2"].hash]
    assert app2.engine.resume_switch() is None


def test_orphan_suspended_then_released_by_parent(make_app, short_fork_docs):
    recording, _ = short_fork_docs
    app = make_app(recording, name="orphan.db")
    by_name = {b["name"]: b["block"] for b in recording["blocks"]}

    # g0 then o1 (whose ancestry is absent).
    app.engine.ingest(by_name["g0"], request_id="req-o-g0")
    r = app.engine.ingest(by_name["o1"], request_id="req-o-o1")
    assert r.outcome == "PENDING"
    assert app.store.pending_count() == 1
    # Unknown-parent diagnostics include parent hash and state.
    row = [x for x in app.store.diagnostics(limit=10) if x["request_id"] == "req-o-o1"][0]
    assert row["parent"] == by_name["o1"]["parent"]
    assert row["active_height"] == 0

    # Restart-style resume before parents exist changes nothing.
    assert app.engine.resume_pending("req-o-resume-early") == []
    assert app.store.pending_count() == 1

    # Feeding f1 (switch), then f2 releases o1 automatically on ingest.
    app.engine.ingest(by_name["m1"], request_id="req-o-m1")
    app.engine.ingest(by_name["m2"], request_id="req-o-m2")
    app.engine.ingest(by_name["m3"], request_id="req-o-m3")
    app.engine.ingest(by_name["f1"], request_id="req-o-f1")
    r2 = app.engine.ingest(by_name["f2"], request_id="req-o-f2")
    assert r2.released
    assert app.store.pending_count() == 0
