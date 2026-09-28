"""Encoding & signature recovery: RLP correctness and secp256k1 recovery.

These exercise the real crypto path: a signed transaction must recover to the
signing address; mutating any signed byte must fail recovery or yield a
different sender. They also cross-check RLP against known byte vectors.
"""

import pytest

from basefee_model.encoding import rlp
from basefee_model.encoding.crypto import (address_from_private, keccak256,
                                           recover_public_key)
from basefee_model.encoding.transaction import (Transaction, decode_transaction)
from basefee_model.errors import EncodingError, FailureCode, SignatureError
from basefee_model.fixtures import sign_eip1559_tx, sign_legacy_tx, wallet


# -- RLP known vectors ------------------------------------------------------
@pytest.mark.parametrize("value,expected", [
    (b"", b"\x80"),
    (b"d", b"d"),
    (b"dog", b"\x83dog"),
    (b"A", b"A"),
])
def test_rlp_short_string_vectors(value, expected):
    assert rlp.encode_raw(value) == expected


def test_rlp_list_known_vectors():
    assert rlp.encode([]) == b"\xc0"
    assert rlp.encode([b"dog", b"cat", b"dog"]) == \
        bytes.fromhex("cc83646f678363617483646f67")


def test_rlp_integer_roundtrip():
    for n in (0, 1, 127, 128, 255, 256, 2 ** 64, 2 ** 200):
        assert rlp.int_from_bytes(rlp.int_to_bytes(n)) == n


def test_rlp_nested_roundtrip():
    obj = [b"a", [b"b", b"c", []], [[]], b""]
    assert rlp.decode(rlp.encode(obj)) == obj


@pytest.mark.parametrize("bad", [b"", b"\x81", b"\xc8\x83dog", b"\xb8",
                                 bytes([0x81]), b"\xc1\x80\xff"])
def test_rlp_rejects_malformed(bad):
    with pytest.raises(EncodingError):
        rlp.decode(bad)


def test_keccak_known_vector():
    # keccak256("") canonical digest
    assert keccak256(b"").hex() == \
        "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"


# -- signature recovery -----------------------------------------------------
def test_signed_eip1559_recovers_signer():
    w = wallet("alice")
    _, raw = sign_eip1559_tx(w, nonce=0, max_fee_per_gas=100,
                             max_priority_fee_per_gas=10, gas_limit=21_000)
    tx = decode_transaction(raw)
    assert tx.sender() == w.address


def test_signed_legacy_recovers_signer():
    w = wallet("bob")
    _, raw = sign_legacy_tx(w, nonce=3, gas_price=200, gas_limit=21_000)
    tx = decode_transaction(raw)
    assert tx.type == 0
    assert tx.sender() == w.address
    # EIP-155 chain id round trips through v.
    assert tx.chain_id == 1559
    assert tx.nonce == 3


def test_signature_recovery_rejects_garbage():
    w = wallet("carol")
    tx, _ = sign_eip1559_tx(w, 0, max_fee_per_gas=1, max_priority_fee_per_gas=1)
    tx.r = b"\x00" * 32  # zero r
    with pytest.raises(SignatureError) as exc:
        tx.sender()
    assert exc.value.code == FailureCode.BAD_SIGNATURE


def test_tampered_payload_changes_or_fails_recovery():
    w = wallet("dave")
    tx, raw = sign_eip1559_tx(w, 0, max_fee_per_gas=100,
                              max_priority_fee_per_gas=10, value=1000)
    # Flip the value byte inside the encoded payload.
    tampered = bytearray(raw)
    # Locate encoded value (0x03e8) and flip a byte.
    idx = raw.find((1000).to_bytes(2, "big"))
    assert idx > 0
    tampered[idx] ^= 0xFF
    tx2 = decode_transaction(bytes(tampered))
    # Recovery either throws or yields a different sender -- never the signer.
    try:
        sender = tx2.sender()
    except SignatureError:
        return
    assert sender != w.address


def test_encoding_roundtrip_preserves_fields():
    w = wallet("erin")
    tx, raw = sign_eip1559_tx(
        w, nonce=7, max_fee_per_gas=1234, max_priority_fee_per_gas=56,
        gas_limit=99_000, value=42, data=bytes([0, 1, 2, 0]))
    decoded = decode_transaction(raw)
    assert decoded.nonce == 7
    assert decoded.max_fee_per_gas == 1234
    assert decoded.max_priority_fee_per_gas == 56
    assert decoded.gas_limit == 99_000
    assert decoded.value == 42
    assert decoded.data == bytes([0, 1, 2, 0])
    assert decoded.sender() == w.address
    assert decoded.encoded() == raw  # canonical encoding


def test_deterministic_fixture_identity():
    # Named identities are stable across instantiations.
    assert wallet("funder").address == wallet("funder").address
    assert wallet("funder").address != wallet("other").address
