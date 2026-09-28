"""Cryptographic layer (Ed25519 signatures + weighted threshold verification).

Uses the ``cryptography`` library's Ed25519. A header is authorized by a
*certificate*: a set of member signatures over a domain-separated message
that binds the exact header bytes. Authorization is by accumulated member
weight, with each member counted at most once.

Failure discipline:
    malformed key/signature input -> SignatureInvalid / CheckpointSignatureInvalid
    genuine cryptographic failure  -> ComputeFailed (never swallowed)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric import ed25519

from . import codec
from .errors import (
    CheckpointSignatureInvalid,
    ComputeFailed,
    SignatureInvalid,
    WeightBelowQuorum,
)
from .types import Certificate, CheckpointEnvelope, Committee, Header


def generate_keypair() -> tuple[bytes, bytes]:
    """Return ``(private_seed, public_key)`` — each 32 bytes."""
    sk = ed25519.Ed25519PrivateKey.generate()
    return sk.private_bytes_raw(), sk.public_key().public_bytes_raw()


def sign_header(private_seed: bytes, header: Header) -> bytes:
    """Produce one committee member's 64-byte signature for a header."""
    try:
        sk = ed25519.Ed25519PrivateKey.from_private_bytes(private_seed)
        return sk.sign(codec.certificate_message(header))
    except (ValueError, TypeError) as exc:
        raise ComputeFailed(f"signing failed: {exc}") from None


def sign_checkpoint(private_seed: bytes, envelope_payload: Any) -> bytes:
    """Sign the canonical checkpoint bytes with the trusted checkpoint key."""
    from .types import Checkpoint

    try:
        sk = ed25519.Ed25519PrivateKey.from_private_bytes(private_seed)
        if isinstance(envelope_payload, Checkpoint):
            message = codec.checkpoint_signing_message(envelope_payload)
        else:  # already canonical bytes
            message = bytes(envelope_payload)
        return sk.sign(message)
    except (ValueError, TypeError) as exc:
        raise ComputeFailed(f"checkpoint signing failed: {exc}") from None


def _verify_one(public_key: bytes, signature: bytes, message: bytes) -> None:
    if len(public_key) != 32 or len(signature) != 64:
        raise SignatureInvalid("bad signature/key length")
    try:
        vk = ed25519.Ed25519PublicKey.from_public_bytes(public_key)
        vk.verify(signature, message)
    except InvalidSignature:
        raise SignatureInvalid("signature does not verify") from None
    except Exception as exc:  # pragma: no cover - library-level compute failure
        raise ComputeFailed(f"signature verification error: {exc}") from None


@dataclass(frozen=True)
class CertificateEvaluation:
    signed_weight: int
    participant_count: int
    signers: tuple[bytes, ...]
    quorum_weight: int

    @property
    def has_quorum(self) -> bool:
        return self.signed_weight >= self.quorum_weight

    def to_detail(self) -> dict[str, Any]:
        return {
            "signed_weight": self.signed_weight,
            "quorum_weight": self.quorum_weight,
            "participant_count": self.participant_count,
        }


def verify_certificate(
    header: Header, cert: Certificate, committee: Committee
) -> CertificateEvaluation:
    """Verify every vote and enforce the weighted quorum.

    Order of checks (each failure is a distinct, typed rejection):
      1. certificate is bound to *this* exact header
      2. no duplicate signer (one member counted once regardless of weight)
      3. every signer is a member of the authorizing committee
      4. every signature verifies over the header signing message
      5. accumulated weight reaches ``committee.quorum_weight``
    """
    expected_digest = codec.header_digest(header)
    if cert.header_digest != expected_digest:
        raise SignatureInvalid(
            "certificate is bound to a different header",
            {
                "cert_digest": codec.hex_(cert.header_digest),
                "header_digest": codec.hex_(expected_digest),
            },
        )

    message = codec.certificate_message(header)
    seen: set[bytes] = set()
    signed_weight = 0
    for i, vote in enumerate(cert.votes):
        if vote.signer in seen:
            raise SignatureInvalid(
                f"duplicate signer at vote {i}",
                {"signer": codec.hex_(vote.signer)},
            )
        seen.add(vote.signer)
        member = committee.member_by_key(vote.signer)
        if member is None:
            raise SignatureInvalid(
                f"signer at vote {i} is not on the authorizing committee",
                {"signer": codec.hex_(vote.signer)},
            )
        _verify_one(member.public_key, vote.signature, message)
        signed_weight += member.weight

    evaluation = CertificateEvaluation(
        signed_weight=signed_weight,
        participant_count=len(seen),
        signers=tuple(sorted(seen)),
        quorum_weight=committee.quorum_weight,
    )
    if not evaluation.has_quorum:
        raise WeightBelowQuorum(
            "certificate weight below quorum",
            evaluation.to_detail(),
        )
    return evaluation


def verify_checkpoint_envelope(
    envelope: CheckpointEnvelope, trusted_checkpoint_key: bytes
) -> None:
    """Verify an out-of-band checkpoint against the pinned trusted public key."""
    if len(trusted_checkpoint_key) != 32:
        raise ComputeFailed("trusted checkpoint key must be 32 bytes")
    message = codec.checkpoint_signing_message(envelope.checkpoint)
    try:
        vk = ed25519.Ed25519PublicKey.from_public_bytes(trusted_checkpoint_key)
        vk.verify(envelope.signature, message)
    except InvalidSignature:
        raise CheckpointSignatureInvalid(
            "checkpoint signature does not verify against the trusted key"
        ) from None
    except Exception as exc:  # pragma: no cover
        raise ComputeFailed(f"checkpoint verification error: {exc}") from None
