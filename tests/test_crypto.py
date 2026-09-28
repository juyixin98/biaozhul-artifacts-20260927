"""Encoding / hashing / signing unit tests (stateless crypto layer)."""
from __future__ import annotations


import pytest

from reorgindex.crypto.encoding import canonical_json
from reorgindex.crypto.hashing import (
    ZERO_HASH,
    block_identity_hash,
    merkle_root,
    pow_satisfied,
    sha256_hex,
)
from reorgindex.crypto.keys import (
    address_from_pubkey,
    address_from_private_key,
    generate_private_key,
    public_key_bytes,
    sign,
    verify,
)
from reorgindex.replay.builder import FixtureKeys, mine
from reorgindex.kernel.models import make_block, make_transfer

pytestmark = pytest.mark.crypto


def test_canonical_json_key_order_and_compactness():
    a = canonical_json({"b": 1, "a": 2})
    assert a == b'{"a":2,"b":1}'


def test_canonical_json_stable_across_dict_reorder():
    body = {"x": 1, "y": {"q": 2, "p": 3}}
    reordered = {"y": {"p": 3, "q": 2}, "x": 1}
    assert canonical_json(body) == canonical_json(reordered)


def test_ed25519_sign_verify_roundtrip():
    key = generate_private_key()
    msg = b"chain-derived-index"
    sig = sign(key, msg)
    assert verify(key.public_key(), sig, msg) is True


def test_ed25519_rejects_tampered_message():
    key = generate_private_key()
    sig = sign(key, b"original")
    assert verify(key.public_key(), sig, b"tampered") is False


def test_ed25519_rejects_foreign_key():
    signer = generate_private_key()
    other = generate_private_key()
    sig = sign(signer, b"payload")
    assert verify(other.public_key(), sig, b"payload") is False


def test_address_is_deterministic_and_distinct():
    k1 = generate_private_key()
    k2 = generate_private_key()
    a1 = address_from_private_key(k1)
    assert a1 == address_from_pubkey(public_key_bytes(k1.public_key()).hex())
    assert a1.startswith("rx1") and len(a1) == len("rx1") + 40
    assert a1 != address_from_private_key(k2)


def test_merkle_known_zero_case_and_duplicated_tail():
    assert merkle_root([]) == ZERO_HASH
    one = sha256_hex(b"x")
    # single element tree collapses to the element itself
    assert merkle_root([one]) == one
    two = sha256_hex(b"y")
    root2 = merkle_root([one, two])
    root3 = merkle_root([one, two, two])  # tail duplicated
    assert root2 != root3
    assert isinstance(root3, str) and len(root3) == 64


def test_merkle_order_sensitive():
    a, b = sha256_hex(b"a"), sha256_hex(b"b")
    assert merkle_root([a, b]) != merkle_root([b, a])


def test_mined_block_passes_pow_and_identity_binds_nonce():
    keys = FixtureKeys.create()
    block, identity = mine(
        height=1,
        parent="a" * 64,
        producer=keys.producer,
        transactions=[
            make_transfer(
                signer=keys.users["alice"],
                nonce=1,
                recipient=keys.addresses["bob"],
                amount=1,
                fee=0,
                fee_recipient=keys.addresses["producer"],
            )
        ],
        difficulty=4,
        timestamp="t",
    )
    assert block_identity_hash(block) == identity
    assert pow_satisfied(identity, 4) is True
    # difficulty 4 implies a fairly small hash value; target = 2^256//4
    assert int(identity, 16) <= (1 << 256) // 4


def test_block_identity_changes_when_parent_changes():
    keys = FixtureKeys.create()
    txs = [
        make_transfer(
            signer=keys.users["alice"],
            nonce=1,
            recipient=keys.addresses["bob"],
            amount=1,
            fee=0,
            fee_recipient=keys.addresses["producer"],
        )
    ]
    b1 = make_block(
        height=1, parent="a" * 64, producer=keys.producer, transactions=txs,
        difficulty=4, timestamp="t", nonce=0,
    )
    b2 = make_block(
        height=1, parent="b" * 64, producer=keys.producer, transactions=txs,
        difficulty=4, timestamp="t", nonce=0,
    )
    assert block_identity_hash(b1) != block_identity_hash(b2)
