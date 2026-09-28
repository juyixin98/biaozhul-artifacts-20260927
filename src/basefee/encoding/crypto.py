"""Cryptography wrapper around the mature, audited ``ecdsa`` library.

Responsibilities (real work, not demo stubs):
* secp256k1 key generation from an explicit seed (deterministic fixtures);
* deterministic ECDSA signatures (RFC 6979) over a caller-supplied digest;
* low-``s`` normalization (BIP-140 / EIP-2 style) with a recovery index;
* public-key recovery from an ECDSA signature;
* strict verification that also rejects malleable high-``s`` signatures.

Recovery index convention
-------------------------
``ecdsa.VerifyingKey.from_public_key_recovery_with_digest`` returns two
candidate points, but their list order is NOT equal to y-coordinate parity
(two candidates may share a parity). We therefore define ``v`` explicitly as
the **index in that candidate list (0 or 1)**, and verification independently
recomputes both candidates and requires that candidate ``v`` matches both the
claimed address and the ECDSA equation. This is verified empirically in
``tests/test_encoding_signing.py``.

The caller hashes the message; this module signs/verifies the digest directly
(``sigdecode_string`` / ``sign_digest_deterministic``), so there is no double
hashing.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from ecdsa import SigningKey, VerifyingKey, SECP256k1
from ecdsa.util import sigencode_string, sigdecode_string

CURVE = SECP256k1
# Half of the curve order: boundary of the canonical low-s range.
HALF_N = CURVE.order // 2


@dataclass(frozen=True)
class RecoveredKey:
    x: int
    y: int

    def raw_xy(self) -> bytes:
        return self.x.to_bytes(32, "big") + self.y.to_bytes(32, "big")


def generate_signing_key(seed: int) -> SigningKey:
    """Deterministically derive a signing key from an integer seed (fixture use)."""
    if not 0 < seed < CURVE.order:
        raise ValueError("seed must be in 1..order-1")
    return SigningKey.from_secret_exponent(seed, curve=CURVE)


def public_raw(signing_key: SigningKey) -> bytes:
    """64-byte x||y public coordinates."""
    point = signing_key.verifying_key.pubkey.point
    return point.x().to_bytes(32, "big") + point.y().to_bytes(32, "big")


def _candidates(r: int, s: int, digest: bytes):
    try:
        return VerifyingKey.from_public_key_recovery_with_digest(
            r.to_bytes(32, "big") + s.to_bytes(32, "big"), digest, curve=CURVE
        )
    except Exception as exc:
        # A garbage/malleated signature often yields R with no valid R point.
        raise _RecoveryFailure(str(exc)) from exc


class _RecoveryFailure(ValueError):
    """Internal: no recoverable point for the supplied (r,s)."""


def sign_digest(signing_key: SigningKey, digest: bytes) -> tuple[int, int, int]:
    """Sign a 32-byte digest; return canonical (r, s, v).

    ``s`` is normalized to the low range; ``v`` is the ecdsa candidate index
    identifying the signer's public key. Signing is deterministic (RFC 6979).
    """
    if len(digest) != 32:
        raise ValueError("digest must be 32 bytes")
    r, s = sigdecode_string(
        signing_key.sign_digest_deterministic(
            digest, hashfunc=hashlib.sha256, sigencode=sigencode_string
        ),
        CURVE.order,
    )
    if s > HALF_N:
        s = CURVE.order - s
    try:
        cands = _candidates(r, s, digest)
    except _RecoveryFailure:
        return False
    signer_point = signing_key.verifying_key.pubkey.point
    for v, vk in enumerate(cands):
        if vk.pubkey.point == signer_point:
            return r, s, v
    raise RuntimeError("signing key not found among recovery candidates")


def recover_pubkey(r: int, s: int, v: int, digest: bytes) -> RecoveredKey:
    """Recover public-key candidate at index ``v`` (ecdsa candidate ordering)."""
    if v not in (0, 1):
        raise ValueError("recovery index v must be 0 or 1")
    if not (1 <= r < CURVE.order) or not (1 <= s < CURVE.order):
        raise ValueError("r,s out of range")
    cands = _candidates(r, s, digest)
    vk = cands[v]
    return RecoveredKey(vk.pubkey.point.x(), vk.pubkey.point.y())

def verify_recovered(r: int, s: int, v: int, digest: bytes, expected_raw_xy: bytes) -> bool:
    """Strict verification.

    True only when:
      * v is a valid index and r,s are in range,
      * s is canonical low-s (rejects E012 malleability),
      * recovered candidate ``v`` equals ``expected_raw_xy``, and
      * ECDSA verification over the digest succeeds for that key.
    """
    if v not in (0, 1):
        return False
    if not (1 <= r < CURVE.order) or not (1 <= s < CURVE.order):
        return False
    if s > HALF_N:
        return False
    try:
        rec = recover_pubkey(r, s, v, digest)
    except Exception:
        return False
    if rec.raw_xy() != expected_raw_xy:
        return False
    vk = VerifyingKey.from_string(expected_raw_xy, curve=CURVE)
    try:
        return vk.verify_digest(
            r.to_bytes(32, "big") + s.to_bytes(32, "big"), digest,
            sigdecode=sigdecode_string,
        )
    except Exception:
        return False
