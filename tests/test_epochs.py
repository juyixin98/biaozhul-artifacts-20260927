"""Epoch snapshot / weight rule unit tests."""
from __future__ import annotations

import pytest

from localffg.epochs import ValidatorRegistry


def _reg() -> ValidatorRegistry:
    reg = ValidatorRegistry(chain_id="c", epoch_length=10)
    reg.add_validator("alice", b"a" * 32, 100, 0)
    reg.set_weight("alice", 2, 150)
    reg.add_validator("dave", b"d" * 32, 80, 2)   # joins epoch 2
    reg.add_validator("erin", b"e" * 32, 60, 0)
    reg.set_weight("erin", 2, 0)                  # exits after epoch 1
    return reg


def test_epoch_of_round_math():
    reg = _reg()
    assert reg.epoch_of_round(0) == 0
    assert reg.epoch_of_round(9) == 0
    assert reg.epoch_of_round(10) == 1
    assert reg.epoch_of_round(25) == 2


def test_weights_come_from_exact_epoch_snapshot():
    reg = _reg()
    assert reg.weight_at_round("alice", 0) == 100
    assert reg.weight_at_round("alice", 9) == 100
    assert reg.weight_at_round("alice", 20) == 150
    assert reg.weight_at_epoch("alice", 1) == 100
    assert reg.weight_at_epoch("alice", 2) == 150
    assert reg.weight_at_epoch("alice", 99) == 150  # last update persists


def test_membership_join_and_exit_semantics():
    reg = _reg()
    assert reg.weight_at_epoch("dave", 0) == 0
    assert not reg.is_active_at_round("dave", 5)
    assert reg.is_active_at_round("dave", 25)
    assert reg.is_active_at_round("erin", 5)
    assert not reg.is_active_at_round("erin", 25)


def test_unknown_validator_distinguished_from_exit():
    reg = _reg()
    assert reg.weight_at_epoch("nobody", 0) is None
    assert reg.exists("nobody") is False
    assert reg.pubkey_of("nobody") is None
    # known-but-exited returns 0, not None
    assert reg.weight_at_epoch("erin", 5) == 0


def test_snapshot_contains_only_active_members():
    reg = _reg()
    ep0 = reg.snapshot_at_epoch(0)
    assert set(ep0) == {"alice", "erin"}
    ep2 = reg.snapshot_at_epoch(2)
    assert set(ep2) == {"alice", "dave"}
    assert reg.total_active_weight_at_epoch(2) == 230


def test_registry_roundtrip_through_json():
    reg = _reg()
    reg2 = ValidatorRegistry.from_json(reg.to_json())
    assert reg2.epoch_of_round(25) == 2
    assert reg2.weight_at_epoch("alice", 2) == 150
    assert reg2.weight_at_epoch("erin", 2) == 0
    assert reg2.pubkey_of("dave") == b"d" * 32


def test_duplicate_registration_rejected():
    reg = ValidatorRegistry("c", 10)
    reg.add_validator("a", b"k" * 32, 1)
    with pytest.raises(ValueError):
        reg.add_validator("a", b"k" * 32, 2)
    with pytest.raises(KeyError):
        reg.set_weight("zzz", 0, 1)
    with pytest.raises(ValueError):
        ValidatorRegistry("c", 0)
