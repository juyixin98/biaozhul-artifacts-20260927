"""Hashing, secp256k1 signatures and Ethereum-address derivation.

Uses mature cryptographic libraries rather than rolling our own primitives:

* Keccak-256  -> pycryptodome (``Crypto.Hash.keccak``)
* secp256k1   -> coincurve (libsecp256k1 bindings), recoverable signatures

These are real, well-maintained libraries; the code here only orchestrates
their APIs into Ethereum's signing/recovery conventions.
"""

from __future__ import annotations

import secrets

from coincurve import PrivateKey, PublicKey
from Crypto.Hash import keccak as _keccak

from ..errors import FailureCode, SignatureError

# secp256k1 curve order for y-parity / v range handling.
SECP256K1_N = (
    0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
)


def keccak256(data: bytes) -> bytes:
    h = _keccak.new(digest_bits=256)
    h.update(data)
    return h.digest()


def generate_private_key(seed: int | bytes | None = None) -> PrivateKey:
    """Generate a key from an explicit ``seed`` (deterministic fixtures) or
    a CSPRNG (production-style). A seeded key MUST still be a valid scalar;
    the seed is reduced mod the curve order and rejected if zero.
    """
    if seed is None:
        return PrivateKey()
    if isinstance(seed, int):
        seed = seed.to_bytes(32, "big")
    # Hash the seed so arbitrary labels produce well-distributed scalars.
    scalar = int.from_bytes(keccak256(seed), "big") % SECP256K1_N
    if scalar == 0:
        raise ValueError("seed produced zero scalar")
    return PrivateKey(scalar.to_bytes(32, "big"))


def public_key_bytes(priv: PrivateKey) -> bytes:
    """Uncompressed 65-byte public key (0x04 || X || Y)."""
    return priv.public_key.format(compressed=False)


def address_from_pubkey(pub: bytes) -> bytes:
    """Ethereum address = last 20 bytes of keccak256(pubkey[1:])."""
    if len(pub) != 65 or pub[0] != 0x04:
        raise SignatureError("expected 65-byte uncompressed public key",
                             code=FailureCode.BAD_SIGNATURE)
    return keccak256(pub[1:])[-20:]


def address_from_private(priv: PrivateKey) -> bytes:
    return address_from_pubkey(public_key_bytes(priv))


def recoverable_sign(digest: bytes, priv: PrivateKey) -> tuple[int, bytes, bytes]:
    """Sign ``digest`` returning ``(recovery_id, r, s)``.

    ``recovery_id`` in {0,1} is the y-parity needed to recover the public key.
    """
    raw = priv.sign_recoverable(digest, hasher=None)  # 65 bytes: r||s||rid
    r = raw[:32]
    s = raw[32:64]
    rid = raw[64]
    if rid not in (0, 1):
        raise SignatureError("unexpected recovery id",
                             code=FailureCode.BAD_SIGNATURE)
    return rid, r, s


def recover_public_key(digest: bytes, r: bytes, s: bytes, recovery_id: int) -> bytes:
    """Recover the 65-byte uncompressed public key, raising on invalid input."""
    if recovery_id not in (0, 1) or len(r) != 32 or len(s) != 32:
        raise SignatureError("malformed signature components",
                             code=FailureCode.BAD_SIGNATURE)
    # Reject the two canonical invalid scalars up front; libsecp256k1 also
    # rejects r,s >= curve order, but we give a precise failure category.
    if int.from_bytes(r, "big") == 0 or int.from_bytes(s, "big") == 0:
        raise SignatureError("zero r/s", code=FailureCode.BAD_SIGNATURE)
    if int.from_bytes(r, "big") >= SECP256K1_N or \
            int.from_bytes(s, "big") >= SECP256K1_N:
        raise SignatureError("r/s out of range", code=FailureCode.BAD_SIGNATURE)
    sig = r + s + bytes([recovery_id])
    try:
        pub = PublicKey.from_signature_and_message(sig, digest, hasher=None)
    except ValueError as exc:
        # coincurve/libsecp256k1 raises ValueError for malformed or
        # non-recoverable input.
        raise SignatureError(f"signature not recoverable: {exc}",
                             code=FailureCode.BAD_SIGNATURE) from exc
    return pub.format(compressed=False)


def random_request_id() -> str:
    return "req_" + secrets.token_hex(8)
