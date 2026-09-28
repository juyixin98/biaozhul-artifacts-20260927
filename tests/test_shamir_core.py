"""Tests for block encoding/decoding and the independent integrity envelope."""
from __future__ import annotations

import dataclasses
import itertools

import pytest

from app.core import shamir
from app.core.envelope import (
    ShareEnvelope,
    fingerprint,
    seal,
    verify_mac,
)
from app.core.field import FieldParams, SECP256K1_P
from conftest import SECRET_A, SECRET_EMPTY, SECRET_LONG


@pytest.mark.parametrize("secret", [
    b"",
    b"a",
    SECRET_A,
    b"m" * 31,
    SECRET_LONG,
    bytes(range(256)) * 3,
])
def test_encode_decode_roundtrip(secret):
    blocks = shamir.encode_secret(secret)
    # every block fits under the prime and is 31-secret-byte aligned
    assert all(0 <= b < SECP256K1_P for b in blocks)
    expected_blocks = max(1, (len(secret) + 30) // 31)
    assert len(blocks) == expected_blocks
    assert shamir.decode_secret(blocks) == secret


def test_empty_secret_is_distinct_from_nonempty():
    # Single zero-length block must not be confused with a 31-zero-byte secret.
    assert shamir.decode_secret(shamir.encode_secret(b"")) == b""
    assert shamir.decode_secret(shamir.encode_secret(b"\x00" * 31)) == b"\x00" * 31
    assert shamir.encode_secret(b"") != shamir.encode_secret(b"\x00" * 31)


@pytest.mark.parametrize("t,n", [(2, 3), (3, 5), (2, 5), (5, 5)])
def test_split_shapes(t, n):
    res = shamir.split_secret(SECRET_A, t, n)
    assert len(res.shares) == n
    block_count = len(shamir.encode_secret(SECRET_A))
    for x, ys in res.shares:
        assert len(ys) == block_count
    xs = [x for x, _ in res.shares]
    assert xs == list(range(1, n + 1))


def test_invalid_split_params():
    with pytest.raises(shamir.SplitError):
        shamir.split_secret(b"x", 4, 3)   # threshold > total
    with pytest.raises(shamir.SplitError):
        shamir.split_secret(b"x", 0, 3)   # threshold zero


def _env(x, ys, t=3, n=5, cid="c"):
    return ShareEnvelope(
        collection_id=cid, threshold=t, total=n, x=x, ys=tuple(ys),
        field=FieldParams(),
    )


def test_mac_accepts_authentic_rejects_tampered():
    key = b"k" * 32
    env = seal(_env(1, [100, 200]), key)
    assert verify_mac(env, key) is True

    # Flip a single y -> MAC must fail.
    tampered = dataclasses.replace(env, ys=(101, 200))
    assert verify_mac(tampered, key) is False

    # Flip bound metadata (collection id / threshold) -> MAC must fail.
    tampered_meta = dataclasses.replace(env, threshold=4)
    assert verify_mac(tampered_meta, key) is False

    # A different key must not validate it.
    assert verify_mac(env, b"other" + b"k" * 27) is False


def test_missing_mac_fails():
    env = _env(1, [1])
    assert verify_mac(env, b"k" * 32) is False


def test_fingerprint_stable_but_distinct_and_secret_free():
    key = b"k" * 32
    e1 = seal(_env(1, [111]), key)
    e1b = seal(_env(1, [111]), key)
    e2 = seal(_env(2, [111]), key)
    fp1 = fingerprint(e1)
    assert fp1 == fingerprint(e1b)            # same share -> same fp
    assert fp1 != fingerprint(e2)             # different x -> different fp
    assert fp1.startswith("sha256:")
    # fingerprint must not embed the raw element or its hex
    assert "111" not in fp1
