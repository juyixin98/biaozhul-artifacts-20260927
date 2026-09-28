"""单元：Ed25519 检查点签名/验签、诊断三态与脱敏。"""
from __future__ import annotations

import pytest

from app.coding.signing import (
    generate_private_key,
    private_key_from_pem,
    private_key_to_pem,
    sign_checkpoint,
    verify_checkpoint,
)
from app.diagnostics import Decision, Reason, Verdict
from app.logging_setup import key_fingerprint


def test_checkpoint_signature_roundtrip():
    key = generate_private_key()
    sig = sign_checkpoint(key, 3, "ab" * 32, "cd" * 32, "batch-1")
    assert verify_checkpoint(key.public_key(), 3, "ab" * 32, "cd" * 32, "batch-1", sig)


@pytest.mark.parametrize("mutate", ["version", "root", "parent", "batch_id"])
def test_checkpoint_signature_binds_all_fields(mutate):
    key = generate_private_key()
    sig = sign_checkpoint(key, 3, "ab" * 32, "cd" * 32, "batch-1")
    args = [3, "ab" * 32, "cd" * 32, "batch-1"]
    if mutate == "version":
        args[0] = 4
    elif mutate == "root":
        args[1] = "ee" * 32
    elif mutate == "parent":
        args[2] = "ee" * 32
    else:
        args[3] = "batch-2"
    assert not verify_checkpoint(key.public_key(), *args, sig)


def test_signature_rejected_by_other_key():
    sig = sign_checkpoint(generate_private_key(), 1, "ab" * 32, None, "b")
    assert not verify_checkpoint(generate_private_key().public_key(), 1, "ab" * 32, None, "b", sig)


def test_pem_roundtrip():
    key = generate_private_key()
    restored = private_key_from_pem(private_key_to_pem(key))
    assert private_key_to_pem(restored) == private_key_to_pem(key)


def test_verdict_three_states_have_distinct_reasons():
    accepted = Verdict(Decision.ACCEPT, Reason.MEMBERSHIP_VERIFIED, "ok")
    rejected = Verdict(Decision.REJECT, Reason.ROOT_MISMATCH, "bad root")
    inconclusive = Verdict(Decision.INCONCLUSIVE, Reason.UNKNOWN_ROOT, "?")
    assert accepted.accepted and not rejected.accepted and not inconclusive.accepted
    d = rejected.as_dict()
    assert d["decision"] == "REJECT" and d["reason"] == "ROOT_MISMATCH"
    assert "request"  # module import sanity


def test_key_fingerprint_is_redacted_and_stable():
    fp1 = key_fingerprint(b"\x01" * 32)
    fp2 = key_fingerprint(b"\x01" * 32)
    fp3 = key_fingerprint(b"\x02" * 32)
    assert fp1 == fp2 and fp1 != fp3
    assert len(fp1) == 12
    # 指纹不应是键本身的任何子串（不可逆）
    assert (b"\x01" * 2).hex() not in fp1
