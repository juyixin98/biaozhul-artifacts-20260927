"""Crypto boundary tests: mature-library Ed25519 verification contract."""

import pytest

from utxo_ledger.crypto import KeyPair, sign_message, verify_signature
from utxo_ledger.errors import ErrorCode, LedgerError

MSG = b"canonical-sighash-bytes"


def test_sign_verify_roundtrip():
    kp = KeyPair.from_seed(b"\x11" * 32)
    sig = kp.sign(MSG)
    assert len(sig) == 64
    verify_signature(kp.public_bytes, MSG, sig)  # no raise == valid


def test_deterministic_seed_keys():
    a = KeyPair.from_seed(b"\x22" * 32)
    b = KeyPair.from_seed(b"\x22" * 32)
    assert a.public_bytes == b.public_bytes
    assert a.sign(MSG) == b.sign(MSG)  # Ed25519 deterministic signatures


def test_tampered_message_is_state_sig_tampered():
    kp = KeyPair.generate()
    sig = kp.sign(MSG)
    with pytest.raises(LedgerError) as ei:
        verify_signature(kp.public_bytes, MSG + b"x", sig)
    assert ei.value.code is ErrorCode.SIG_TAMPERED
    assert ei.value.category.value == "state"


def test_wrong_key_signature_rejected_as_tampered():
    signer = KeyPair.generate()
    other = KeyPair.generate()
    sig = signer.sign(MSG)
    with pytest.raises(LedgerError) as ei:
        verify_signature(other.public_bytes, MSG, sig)
    assert ei.value.code is ErrorCode.SIG_TAMPERED


def test_malformed_material_is_input_category():
    kp = KeyPair.generate()
    with pytest.raises(LedgerError) as ei:
        verify_signature(b"\x00" * 31, MSG, kp.sign(MSG))
    assert ei.value.code is ErrorCode.BAD_PUBLIC_KEY
    assert ei.value.category.value == "input"
    with pytest.raises(LedgerError) as ei2:
        verify_signature(kp.public_bytes, MSG, b"\x00" * 63)
    assert ei2.value.code is ErrorCode.BAD_SIGNATURE
    assert ei2.value.category.value == "input"


def test_flip_one_signature_byte_invalidates():
    kp = KeyPair.generate()
    sig = bytearray(kp.sign(MSG))
    sig[7] ^= 0x01
    with pytest.raises(LedgerError):
        verify_signature(kp.public_bytes, MSG, bytes(sig))


def test_sign_message_helper_accepts_seed():
    sig = sign_message(b"\x33" * 32, MSG)
    kp = KeyPair.from_seed(b"\x33" * 32)
    verify_signature(kp.public_bytes, MSG, sig)
