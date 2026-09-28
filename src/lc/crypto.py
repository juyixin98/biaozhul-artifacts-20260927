"""Cryptography layer: Ed25519 primitives and weighted committee certificate
verification, built on the mature ``cryptography`` library.

A *threshold signature* in this simplified protocol is a set of individual
Ed25519 signatures over the domain-separated certificate message::

    M = DOM_CERT_SIGN || header_root

Verification rules (all enforced here or by the caller contract):

1. Every signer MUST be a member of the authorizing committee; an unknown key
   is ``SIGNER_UNKNOWN`` (a signer from the *old* committee signing a header
   that rotates to a new committee is exactly this case).
2. Every signature is verified individually against the signer's public key
   with Ed25519 verify; one bad signature is ``CRYPTO_BAD_SIGNATURE``.
3. Duplicate signers inside one certificate are rejected at parsing time.
4. Only *distinct* known members' weights are summed. Certificate replay or
   signature padding therefore cannot inflate the weight.
5. Supermajority: ``weight >= floor(2*total_weight/3) + 1``. This is the
   quorum for the committee that *authorizes* a header (incl. its rotation).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from . import encoding
from .errors import Code, LightClientError
from .types import Certificate, Committee

# Cache parsed public keys: 32 raw bytes -> Ed25519PublicKey object.
_pubkey_cache: Dict[bytes, Ed25519PublicKey] = {}


@dataclass
class CertificateVerification:
    """Detailed result of an (attempted) certificate verification."""

    verified: bool
    message: bytes
    total_weight: int
    required_weight: int
    signed_weight: int
    distinct_signers: int
    accepted_signers: List[bytes] = field(default_factory=list)
    # Present (False) when a cryptographic check failed.
    failure_code: Optional[str] = None
    failure_detail: Optional[str] = None


def threshold_weight(total_weight: int) -> int:
    """floor(2W/3)+1 — the BFT supermajority of the authorizing committee."""
    if total_weight <= 0:
        raise ValueError("committee total weight must be positive")
    return (2 * total_weight) // 3 + 1


def generate_keypair() -> tuple:
    """Test-fixture helper: returns (private_key_obj, 32-byte raw public key)."""
    sk = Ed25519PrivateKey.generate()
    pk = sk.public_key()
    raw_pk = pk.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return sk, raw_pk


def private_key_raw(sk: Ed25519PrivateKey) -> bytes:
    return sk.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )


def load_private_key(raw: bytes) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(raw)


def load_public_key(raw: bytes) -> Ed25519PublicKey:
    cached = _pubkey_cache.get(raw)
    if cached is not None:
        return cached
    if len(raw) != 32:
        raise LightClientError(
            Code.MALFORMED_COMMITTEE,
            f"public key must be 32 bytes, got {len(raw)}",
        )
    try:
        pk = Ed25519PublicKey.from_public_bytes(raw)
    except Exception as exc:  # invalid curve/encoding bytes
        raise LightClientError(
            Code.MALFORMED_COMMITTEE,
            f"invalid Ed25519 public key: {exc}",
        )
    _pubkey_cache[raw] = pk
    return pk


def sign_header(sk: Ed25519PrivateKey, root: bytes) -> bytes:
    return sk.sign(encoding.certificate_message(root))


def verify_individual(pk_raw: bytes, signature: bytes, message: bytes) -> bool:
    """Boolean Ed25519 verify; never raises for a bad signature."""
    try:
        pk = load_public_key(pk_raw)
        pk.verify(signature, message)
        return True
    except (InvalidSignature, LightClientError, ValueError, TypeError):
        return False


def verify_certificate(
    certificate: Certificate,
    committee: Committee,
    expected_header_root: bytes,
) -> CertificateVerification:
    """Verify a weighted committee certificate.

    Raises ``LightClientError`` (SIGNER_UNKNOWN / CRYPTO_BAD_SIGNATURE) on the
    first hard failure so the kernel can attach a precise rejection code.
    A *structurally valid but under-weight* certificate returns a result with
    ``verified=False`` and signed/required weights — the kernel maps that to
    INSUFFICIENT_WEIGHT with the numbers in the details.
    """
    if certificate.header_root != expected_header_root:
        # Caller normally checks this first (binding), kept as a defence.
        raise LightClientError(
            Code.CERT_BIND_MISMATCH,
            "certificate is bound to a different header root",
            details={
                "cert_root": certificate.header_root.hex(),
                "header_root": expected_header_root.hex(),
            },
        )

    weight_by_key = {m.public_key: m.weight for m in committee.members}
    total = committee.total_weight
    required = threshold_weight(total)
    message = encoding.certificate_message(expected_header_root)

    signed_weight = 0
    accepted: List[bytes] = []
    for index, entry in enumerate(certificate.signatures):
        if entry.public_key not in weight_by_key:
            raise LightClientError(
                Code.SIGNER_UNKNOWN,
                f"signer at index {index} is not a member of the authorizing "
                "committee",
                details={"index": index, "public_key": "0x" + entry.public_key.hex()},
            )
        if not verify_individual(entry.public_key, entry.signature, message):
            raise LightClientError(
                Code.CRYPTO_BAD_SIGNATURE,
                f"invalid signature at index {index}",
                details={"index": index, "public_key": "0x" + entry.public_key.hex()},
            )
        accepted.append(entry.public_key)
        signed_weight += weight_by_key[entry.public_key]

    verified = signed_weight >= required
    result = CertificateVerification(
        verified=verified,
        message=message,
        total_weight=total,
        required_weight=required,
        signed_weight=signed_weight,
        distinct_signers=len(accepted),
        accepted_signers=accepted,
    )
    if not verified:
        result.failure_code = Code.INSUFFICIENT_WEIGHT.value
        result.failure_detail = (
            f"signed weight {signed_weight} < required {required} "
            f"(total {total})"
        )
    return result
