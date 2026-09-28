"""Encoding & signing: RLP canonical encoding and real secp256k1 signatures."""

from __future__ import annotations

import pytest

from basefee.encoding import rlp
from basefee.encoding.hexutil import encode_int, decode_int, hex_to_bytes
from basefee.encoding import crypto, serialization as ser
from basefee.errors import ErrorCode


# Canonical RLP vectors, including nested lists and long-form strings.
@pytest.mark.parametrize("raw,expected", [
    (b"", "0x80"),
    (b"d", "0x64"),
    (b"dog", "0x83646f67"),
    ([b"cat", b"dog"], "0xc88363617483646f67"),
    ([b"zw", [b"4"], b"wg"], "0xc8827a77c134827767"),
    ([], "0xc0"),
    ([b"", b"", b"", []], "0xc4808080c0"),
    ([[[]]], "0xc2c1c0"),
    ([b"a" * 56], "0xf83ab838" + "61" * 56),
    ([b"a" * 57], "0xf83bb839" + "61" * 57),
])
def test_rlp_standard_vectors(raw, expected):
    assert "0x" + rlp.encode(raw).hex() == expected


def test_rlp_roundtrip_and_scalar():
    payload = [b"", b"x", [b"y", b"zz"], encode_int(1_000_000), b""]
    out = rlp.decode(rlp.encode(payload))
    assert out == payload
    assert decode_int(out[3]) == 1_000_000
    # zero encodes to the empty byte string per RLP scalar convention
    assert rlp.encode(encode_int(0)) == b"\x80"


def test_rlp_rejects_noncanonical():
    # single byte < 0x80 must be bare; long string form is non-canonical
    with pytest.raises(rlp.RLPError):
        rlp.decode(bytes([0x81, 0x01]))
    with pytest.raises(rlp.RLPError):
        rlp.decode(bytes([0xB8, 0x00]))  # long form where short is required
    with pytest.raises(rlp.RLPError):
        rlp.decode(b"\x00\x00")  # trailing bytes


def test_sign_recover_verify_roundtrip(oracle):
    sk = oracle.oracle_key(42)
    raw = oracle.oracle_pub_raw(sk)
    digest = b"\x11" * 32
    r, s, v = crypto.sign_digest(sk, digest)
    rec = crypto.recover_pubkey(r, s, v, digest)
    assert rec.raw_xy() == raw
    assert crypto.verify_recovered(r, s, v, digest, raw) is True
    # wrong digest fails
    assert crypto.verify_recovered(r, s, v, b"\x22" * 32, raw) is False


def test_signatures_are_low_s_and_deterministic(oracle):
    from basefee.encoding.crypto import HALF_N
    sk = oracle.oracle_key(7)
    digest = b"\xab" * 32
    r1, s1, v1 = crypto.sign_digest(sk, digest)
    r2, s2, v2 = crypto.sign_digest(sk, digest)
    assert (r1, s1, v1) == (r2, s2, v2)
    assert 1 <= s1 <= HALF_N
    assert v1 in (0, 1)


def test_high_s_forgery_is_rejected(oracle):
    sk = oracle.oracle_key(7)
    raw = oracle.oracle_pub_raw(sk)
    digest = b"\xab" * 32
    r, s, v = crypto.sign_digest(sk, digest)
    # Malleate s to its high-s mirror
    s_hi = crypto.CURVE.order - s
    assert crypto.verify_recovered(r, s_hi, v ^ 1, digest, raw) is False


def test_signed_transaction_address_recovery_matches_oracle(oracle, sign, keys):
    sk, addr = keys["alice"]
    to = keys["carol"][1]
    wire_tx = sign(sk, max_fee=2_000_000_000, max_tip=100_000_000,
                   gas=21000, to=to, value=123, nonce=0)
    from basefee.api.wire import structured_to_transaction
    tx = structured_to_transaction(wire_tx)
    sender, err = __import__("basefee.kernel.execution", fromlist=["recover_sender"]).recover_sender(tx)
    assert err is None
    assert sender == addr
    # tx hash is stable
    h1 = ser.signed_tx_hash(
        chain_id=tx.chain_id, nonce=tx.nonce, max_fee_per_gas=tx.max_fee_per_gas,
        max_priority_fee_per_gas=tx.max_priority_fee_per_gas, gas_limit=tx.gas_limit,
        to=hex_to_bytes(tx.to), value=tx.value, data=tx.data,
        r=tx.signature.r, s=tx.signature.s, v=tx.signature.v)
    h2 = ser.signed_tx_hash(
        chain_id=tx.chain_id, nonce=tx.nonce, max_fee_per_gas=tx.max_fee_per_gas,
        max_priority_fee_per_gas=tx.max_priority_fee_per_gas, gas_limit=tx.gas_limit,
        to=hex_to_bytes(tx.to), value=tx.value, data=tx.data,
        r=tx.signature.r, s=tx.signature.s, v=tx.signature.v)
    assert h1 == h2 and len(h1) == 66


def test_corrupted_signature_returns_E011(sign, keys):
    sk, _ = keys["alice"]
    wire = sign(sk, max_fee=2_000_000_000, max_tip=1, gas=21000,
                to=keys["carol"][1], value=1)
    wire["signature"]["r"] = str(12345)
    from basefee.api.wire import structured_to_transaction
    from basefee.kernel.execution import recover_sender
    tx = structured_to_transaction(wire)
    sender, err = recover_sender(tx)
    assert err == ErrorCode.E011_SIGNATURE_INVALID.value
