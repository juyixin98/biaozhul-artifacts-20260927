"""Stateless block/transaction validation failure-category tests."""
from __future__ import annotations

import copy

import pytest

from tests.conftest import load_recording
from reorgindex.kernel.errors import IngestionError, RejectReason
from reorgindex.kernel.validation import verify_block, verify_transaction

pytestmark = pytest.mark.kernel

REC = load_recording("short_fork")
PRODUCERS = {REC["producer_address"]}
BLOCKS = {b["name"]: b["block"] for b in REC["blocks"]}
ALLOWED = {4, 16}


def _valid_m1():
    return copy.deepcopy(BLOCKS["m1"])


def test_valid_fixture_block_passes_verification():
    identity = verify_block(_valid_m1(), allowed_difficulties=ALLOWED, authorized_producers=PRODUCERS)
    assert isinstance(identity, str) and len(identity) == 64


def test_bad_tx_signature_is_BAD_SIGNATURE():
    block = _valid_m1()
    tx = block["transactions"][0]
    sig = bytearray.fromhex(tx["signature"])
    sig[0] ^= 0xFF
    tx["signature"] = sig.hex()
    with pytest.raises(IngestionError) as exc:
        verify_transaction(tx, height=1)
    assert exc.value.reason is RejectReason.BAD_SIGNATURE


def test_tx_body_tamper_fails_signature():
    block = _valid_m1()
    tx = block["transactions"][0]
    tx["amount"] = "999"  # body no longer matches signature (and txid)
    with pytest.raises(IngestionError) as exc:
        verify_transaction(tx, height=1)
    assert exc.value.reason is RejectReason.HEADER_MISMATCH


def test_pow_signature_tamper_is_BAD_SIGNATURE():
    block = _valid_m1()
    sig = bytearray.fromhex(block["pow_signature"])
    sig[-1] ^= 0x01
    block["pow_signature"] = sig.hex()
    with pytest.raises(IngestionError) as exc:
        verify_block(block, allowed_difficulties=ALLOWED, authorized_producers=PRODUCERS)
    assert exc.value.reason is RejectReason.BAD_SIGNATURE


def test_nonce_tamper_without_reseal_is_BAD_POW_or_BAD_SIGNATURE():
    # Changing nonce invalidates the PoW; the producer signature also fails
    # because it signs the recomputed identity hash.
    block = _valid_m1()
    block["nonce"] = str(int(block["nonce"]) + 1_000_003)
    with pytest.raises(IngestionError) as exc:
        verify_block(block, allowed_difficulties=ALLOWED, authorized_producers=PRODUCERS)
    assert exc.value.reason in (RejectReason.BAD_POW, RejectReason.BAD_SIGNATURE)


def test_unknown_producer_rejected():
    block = _valid_m1()
    with pytest.raises(IngestionError) as exc:
        verify_block(block, allowed_difficulties=ALLOWED, authorized_producers={"rx1" + "0" * 40})
    assert exc.value.reason is RejectReason.UNKNOWN_PRODUCER


def test_disallowed_difficulty_rejected():
    block = _valid_m1()
    # A difficulty value outside the consensus set is rejected even though the
    # block was mined at a different target.
    block["difficulty"] = 8
    with pytest.raises(IngestionError) as exc:
        verify_block(block, allowed_difficulties=ALLOWED, authorized_producers=PRODUCERS)
    assert exc.value.reason is RejectReason.BAD_DIFFICULTY


def test_bad_merkle_root_rejected():
    block = _valid_m1()
    block["merkle_root"] = "f" * 64
    with pytest.raises(IngestionError) as exc:
        verify_block(block, allowed_difficulties=ALLOWED, authorized_producers=PRODUCERS)
    assert exc.value.reason is RejectReason.BAD_MERKLE


def test_duplicate_txid_within_block_rejected():
    block = _valid_m1()
    block["transactions"].append(copy.deepcopy(block["transactions"][0]))
    # merkle now recomputed as would a sloppy attacker; duplicate check must
    # still trigger
    from reorgindex.crypto.hashing import merkle_root
    block["merkle_root"] = merkle_root([t["txid"] for t in block["transactions"]])
    with pytest.raises(IngestionError) as exc:
        verify_block(block, allowed_difficulties=ALLOWED, authorized_producers=PRODUCERS)
    assert exc.value.reason is RejectReason.DUPLICATE_TXID


def test_mint_outside_genesis_rejected():
    genesis = copy.deepcopy(BLOCKS["g0"])
    mint_tx = genesis["transactions"][0]
    with pytest.raises(IngestionError) as exc:
        verify_transaction(mint_tx, height=1)
    assert exc.value.reason is RejectReason.MINT_OUTSIDE_GENESIS


def test_transfer_in_genesis_rejected():
    tx = copy.deepcopy(BLOCKS["m1"]["transactions"][0])
    with pytest.raises(IngestionError) as exc:
        verify_transaction(tx, height=0)
    assert exc.value.reason is RejectReason.MINT_AT_GENESIS_REQUIRED


def test_genesis_parent_must_be_zero():
    block = copy.deepcopy(BLOCKS["g0"])
    block["parent"] = "1" * 64
    with pytest.raises(IngestionError) as exc:
        verify_block(block, allowed_difficulties=ALLOWED, authorized_producers=PRODUCERS)
    assert exc.value.reason is RejectReason.MALFORMED


def test_missing_field_is_malformed():
    block = _valid_m1()
    del block["timestamp"]
    with pytest.raises(IngestionError) as exc:
        verify_block(block, allowed_difficulties=ALLOWED, authorized_producers=PRODUCERS)
    assert exc.value.reason is RejectReason.MALFORMED
