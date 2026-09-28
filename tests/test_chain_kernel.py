"""Chain kernel: signing, recovery, state transitions, exact failure classes."""
from __future__ import annotations

import pytest

from abibackend.abi import function_selector
from abibackend.chain import (
    BadSignature,
    ChainError,
    InsufficientBalance,
    InvalidCalldata,
    NonceMismatch,
    apply_transaction,
    build_transaction,
    make_bootstrap_state,
)
from abibackend.chain.kernel import SELECTORS
from abibackend.crypto import generate_privkey, privkey_address
from abibackend.replay.fixtures import make_actors

CHAIN = 264


@pytest.fixture(scope="module")
def actors():
    return make_actors(264)


@pytest.fixture
def state(actors):
    from abibackend.replay.fixtures import bootstrap

    return bootstrap(CHAIN, actors)


def test_address_derives_from_privkey(actors):
    assert privkey_address(actors.key_for(actors.alice)) == actors.alice.to_bytes(20, "big")


def test_happy_transfer(state, actors):
    tx = build_transaction(CHAIN, 0, actors.alice, "transfer",
                           [actors.bob, 10**18], 1, actors.key_for(actors.alice))
    before = state.account(actors.bob).balance
    rcpt = apply_transaction(state, tx)
    assert rcpt.ok is True
    assert rcpt.call == "transfer"
    assert rcpt.events[0].name == "Transfer"
    assert state.account(actors.bob).balance == before + 10**18
    assert state.account(actors.alice).nonce == 1


def test_nonce_mismatch_is_specific(state, actors):
    tx = build_transaction(CHAIN, 5, actors.alice, "transfer", [actors.bob, 1], 1,
                           actors.key_for(actors.alice))
    with pytest.raises(NonceMismatch):
        apply_transaction(state, tx)
    # failure must not mutate nonce/balance
    assert state.account(actors.alice).nonce == 0


def test_bad_signature_on_tamper(state, actors):
    tx = build_transaction(CHAIN, 0, actors.alice, "transfer", [actors.bob, 1], 1,
                           actors.key_for(actors.alice))
    tx.calldata = tx.calldata[:-1] + bytes([tx.calldata[-1] ^ 0xFF])
    with pytest.raises(BadSignature):
        apply_transaction(state, tx)
    assert state.account(actors.alice).nonce == 0


def test_insufficient_balance(state, actors):
    # carol has zero balance in a fresh state
    tx = build_transaction(CHAIN, 0, actors.carol, "transfer",
                           [actors.bob, 1], 1, actors.key_for(actors.carol))
    with pytest.raises(InsufficientBalance):
        apply_transaction(state, tx)


def test_invalid_calldata_truncated(state, actors):
    from abibackend.chain import Transaction
    from abibackend.crypto import Signature, sign_digest

    selector = function_selector("transfer", ["address", "uint256"])
    calldata = selector + actors.bob.to_bytes(32, "big")  # missing amount word
    tx = Transaction(CHAIN, 0, actors.alice, calldata, 1, Signature(0, 0, 0))
    tx.signature = sign_digest(actors.key_for(actors.alice), tx.signing_hash())
    with pytest.raises(InvalidCalldata) as exc:
        apply_transaction(state, tx)
    # The wrapped message carries the precise codec category.
    assert "offset_out_of_bounds" in str(exc.value)


def test_unknown_selector(state, actors):
    from abibackend.chain import Transaction
    from abibackend.crypto import Signature, sign_digest

    calldata = b"\xde\xad\xbe\xef" + b"\x00" * 64
    tx = Transaction(CHAIN, 0, actors.alice, calldata, 1, Signature(0, 0, 0))
    tx.signature = sign_digest(actors.key_for(actors.alice), tx.signing_hash())
    with pytest.raises(InvalidCalldata):
        apply_transaction(state, tx)


def test_wrong_chain_id(state, actors):
    tx = build_transaction(999, 0, actors.alice, "transfer", [actors.bob, 1], 1,
                           actors.key_for(actors.alice))
    with pytest.raises(ChainError):
        apply_transaction(state, tx)


def test_approve_and_transfer_from(state, actors):
    approve = build_transaction(CHAIN, 0, actors.alice, "approve",
                                [actors.carol, 7**18], 1, actors.key_for(actors.alice))
    apply_transaction(state, approve)
    pull = build_transaction(CHAIN, 0, actors.carol, "transferFrom",
                             [actors.alice, actors.carol, 7**18], 1,
                             actors.key_for(actors.carol))
    rcpt = apply_transaction(state, pull)
    assert rcpt.ok
    assert state.account(actors.carol).balance == 7**18
    # allowance consumed
    assert state.account(actors.alice).allowances[actors.carol] == 0


def test_selectors_present():
    assert len(SELECTORS) == 3
