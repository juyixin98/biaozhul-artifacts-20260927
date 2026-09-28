"""Unit tests for encoding and hashing: domain separation and empty levels."""
from __future__ import annotations

import pytest

from smt.crypto import (
    KEY_BYTES,
    bit_at,
    canonical_json,
    empty_at,
    encode_branch,
    encode_leaf,
    normalize_key,
    sha256,
    sign_payload,
    verify_payload,
)


def test_keys_are_fixed_256_bit():
    k = normalize_key("00" * 32)
    assert len(k) == KEY_BYTES == 32
    assert normalize_key("0x" + "ab" * 32) == bytes.fromhex("ab" * 32)
    for bad in ["00" * 31, "zz" * 32, "00" * 33, "abc"]:
        with pytest.raises(ValueError):
            normalize_key(bad)
    with pytest.raises(TypeError):
        normalize_key(123)


def test_leaf_binds_both_key_and_value():
    k1, k2 = bytes(32), bytes([1] + [0] * 31)
    assert encode_leaf(k1, b"v") != encode_leaf(k2, b"v")
    assert encode_leaf(k1, b"v") != encode_leaf(k1, b"w")
    # present empty value is a distinct leaf commitment
    assert sha256(encode_leaf(k1, b"")) != sha256(encode_leaf(k1, b"x"))


def test_domain_tags_never_collide():
    k = bytes(32)
    leaf = encode_leaf(k, b"")
    branch = encode_branch(empty_at(1), empty_at(1))
    assert leaf[0] != branch[0]
    # every empty level is distinct and cannot equal a leaf/branch preimage
    levels = {empty_at(d) for d in range(257)}
    assert len(levels) == 257
    assert empty_at(0) not in {sha256(leaf), sha256(branch)}


def test_empty_hashes_are_defined_level_by_level():
    # base slot and first derived level differ
    assert empty_at(256) != empty_at(255)
    # level 255 must bind TWO copies of level 256 (not just a tag)
    import hashlib

    from smt.crypto.encoding import TAG_EMPTY
    pre = TAG_EMPTY + (255).to_bytes(2, "big") + empty_at(256) + empty_at(256)
    assert empty_at(255) == hashlib.sha256(pre).digest()


def test_bit_ordering():
    k = bytes([0b10000000] + [0] * 31)
    assert bit_at(k, 0) == 1
    assert bit_at(k, 1) == 0
    k2 = bytes([0] * 31 + [0b00000001])
    assert bit_at(k2, 255) == 1
    assert bit_at(k2, 254) == 0


def test_journal_hmac_roundtrip_and_tamper():
    payload = {"a": 1, "b": "x"}
    sig = sign_payload(payload, "secret")
    assert verify_payload(payload, sig, "secret") is True
    assert verify_payload(payload, sig, "other") is False
    assert verify_payload({**payload, "a": 2}, sig, "secret") is False
    assert verify_payload(payload, "deadbeef", "secret") is False


def test_canonical_json_is_stable():
    assert canonical_json({"b": 1, "a": [1, 2]}) == canonical_json({"a": [1, 2], "b": 1})
