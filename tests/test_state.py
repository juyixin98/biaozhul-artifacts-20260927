"""Chain-state machine tests: nonces, balances, duplicate-txid semantics."""
from __future__ import annotations

import copy

import pytest

from tests.conftest import load_recording
from reorgindex.kernel.errors import IngestionError, RejectReason
from reorgindex.kernel.state import ChainState

pytestmark = pytest.mark.kernel

REC = load_recording("short_fork")
BLOCKS = {b["name"]: b["block"] for b in REC["blocks"]}


def _genesis_state() -> ChainState:
    state = ChainState()
    state.apply_block(BLOCKS["g0"])
    return state


def test_genesis_mints_seed_balances():
    state = _genesis_state()
    addrs = REC["addresses"]
    assert state.account(addrs["alice"]).balance == 1_000
    assert state.account(addrs["bob"]).balance == 500
    assert state.account(addrs["carol"]).balance == 0


def test_transfer_updates_balance_and_nonce():
    state = _genesis_state()
    state.apply_block(BLOCKS["f1"])  # alice -> carol 100 (weighted fork block)
    addrs = REC["addresses"]
    assert state.account(addrs["alice"]).balance == 900
    assert state.account(addrs["alice"]).nonce == 1
    assert state.account(addrs["carol"]).balance == 100


def test_same_txid_twice_in_one_chain_is_DUPLICATE_TXID():
    state = _genesis_state()
    state.apply_block(BLOCKS["f1"])
    # Replaying the identical transaction body on a second block must fail:
    # a transaction present on both branches cannot contribute twice to a
    # single chain view.
    evil = copy.deepcopy(BLOCKS["f1"])
    evil["height"] = 2
    with pytest.raises(IngestionError) as exc:
        state.apply_block(evil)
    assert exc.value.reason is RejectReason.DUPLICATE_TXID


def test_nonce_must_be_strictly_sequential():
    state = _genesis_state()
    # m2 is bob->alice (bob nonce 1) and is valid on its own; instead build a
    # gappy alice transaction (nonce 3 without 1/2) using the raw fixture f2,
    # whose transfer carries alice nonce 2 on a chain where alice has none.
    with pytest.raises(IngestionError) as exc:
        state.apply_block(BLOCKS["f2"])
    assert exc.value.reason is RejectReason.BAD_NONCE_ORDER
    assert exc.value.state["expected"] == 1 and exc.value.state["got"] == 2


def test_nonce_reuse_category_when_already_spent():
    state = _genesis_state()
    state.apply_block(BLOCKS["f1"])  # alice nonce 1 consumed
    evil = copy.deepcopy(BLOCKS["f1"])
    evil["height"] = 2
    with pytest.raises(IngestionError) as exc:
        state.apply_block(evil)
    assert exc.value.reason is RejectReason.DUPLICATE_TXID


def test_state_overspend_signed():
    from reorgindex.replay.builder import BranchBuilder, FixtureKeys

    keys = FixtureKeys.create()
    main = BranchBuilder(keys, difficulty=4)
    main.genesis()
    branch = main.snapshot(tip_name="g0")
    branch.nonces.clear()
    over = branch.make_transfer(sender="alice", recipient="bob", amount=10_000, nonce=1)
    branch.child_with_txs(name="x", transactions=[over])

    state = ChainState()
    state.apply_block(main.blocks["g0"])
    with pytest.raises(IngestionError) as exc:
        state.apply_block(branch.blocks["x"])
    assert exc.value.reason is RejectReason.INSUFFICIENT_FUNDS
    assert exc.value.state["balance"] == 1_000
    assert exc.value.state["required"] == 10_000


def test_clone_is_independent():
    state = _genesis_state()
    clone = state.clone()
    clone.apply_block(BLOCKS["f1"])
    # original untouched
    addrs = REC["addresses"]
    assert state.account(addrs["carol"]).balance == 0
    assert clone.account(addrs["carol"]).balance == 100
