"""Signing / verification primitives.

Uses Ed25519 from the well-audited `cryptography` library (pure EdDSA,
deterministic). The signature is over the canonical payload built in
`encoding.encode_signed_vote_payload`, so it is bound to:

    chain domain || chain id || validator id || source round ||
    target round || block root

A Signer holds the private key (test/local fixtures only — synthetic keys,
never production material). Only public verification material is ever stored
or shipped in evidence.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .encoding import encode_signed_vote_payload, vote_digest
from .models import SignedVote, Vote


@dataclass(frozen=True)
class VerificationResult:
    ok: bool
    reason: str | None = None  # populated when ok is False


class Signer:
    """Local synthetic validator signer."""

    def __init__(self, validator_id: str, private_key: Ed25519PrivateKey):
        self.validator_id = validator_id
        self._private_key = private_key
        self.public_key_bytes = private_key.public_key().public_bytes_raw()

    @classmethod
    def generate(cls, validator_id: str) -> "Signer":
        return cls(validator_id, Ed25519PrivateKey.generate())

    @classmethod
    def from_seed(cls, validator_id: str, seed_material: bytes) -> "Signer":
        """Deterministic key from seed material (sha512-expanded by Ed25519)."""
        digest = hashlib.sha256(seed_material).digest()
        return cls(validator_id, Ed25519PrivateKey.from_private_bytes(digest))

    def private_pem(self) -> bytes:
        return self._private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )

    def sign_vote(
        self,
        *,
        domain: bytes,
        chain_id: str,
        source_round: int,
        target_round: int,
        block_root: bytes,
    ) -> SignedVote:
        payload = encode_signed_vote_payload(
            domain=domain,
            chain_id=chain_id,
            validator_id=self.validator_id,
            source_round=source_round,
            target_round=target_round,
            block_root=block_root,
        )
        signature = self._private_key.sign(payload)
        vote = Vote(
            chain_id=chain_id,
            validator_id=self.validator_id,
            source_round=source_round,
            target_round=target_round,
            block_root=block_root,
        )
        return SignedVote(vote=vote, signer_pubkey=self.public_key_bytes, signature=signature)


def canonical_payload_for(signed: SignedVote, domain: bytes) -> bytes:
    v = signed.vote
    return encode_signed_vote_payload(
        domain=domain,
        chain_id=v.chain_id,
        validator_id=v.validator_id,
        source_round=v.source_round,
        target_round=v.target_round,
        block_root=v.block_root,
    )


def signed_vote_digest(signed: SignedVote, domain: bytes) -> bytes:
    return vote_digest(canonical_payload_for(signed, domain))


def verify_signed_vote(
    signed: SignedVote,
    domain: bytes,
    *,
    expected_pubkey: bytes | None = None,
) -> VerificationResult:
    """Verify envelope shape + Ed25519 signature.

    `expected_pubkey` (the key recorded in the validator registry snapshot)
    must match the pubkey embedded in the envelope — otherwise the envelope
    could claim an arbitrary signer.
    """
    v = signed.vote
    if len(signed.signer_pubkey) != 32:
        return VerificationResult(False, "pubkey_length")
    if len(signed.signature) != 64:
        return VerificationResult(False, "signature_length")
    if expected_pubkey is not None and signed.signer_pubkey != expected_pubkey:
        return VerificationResult(False, "pubkey_mismatch_registry")
    try:
        payload = canonical_payload_for(signed, domain)
        pub = Ed25519PublicKey.from_public_bytes(signed.signer_pubkey)
        pub.verify(signed.signature, payload)
    except InvalidSignature:
        return VerificationResult(False, "bad_signature")
    except Exception as exc:  # malformed key material etc. — explicit, not "ok"
        return VerificationResult(False, f"verify_error:{type(exc).__name__}")
    return VerificationResult(True)
