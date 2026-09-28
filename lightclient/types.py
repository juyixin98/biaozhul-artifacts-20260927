"""Core data model: committee, header, certificate, checkpoint.

These are plain value types. Structural validation happens in
``lightclient.codec`` (wire boundaries) and in ``lightclient.kernel``
(protocol rules); types here only enforce representation-level invariants.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Member:
    #: 32-byte Ed25519 public key.
    public_key: bytes
    weight: int

    def __post_init__(self) -> None:
        if not isinstance(self.public_key, (bytes, bytearray)):
            raise TypeError("public_key must be bytes")
        if len(self.public_key) != 32:
            raise ValueError("public_key must be 32 bytes")
        if not isinstance(self.weight, int) or isinstance(self.weight, bool):
            raise TypeError("weight must be an int")
        if self.weight < 1:
            raise ValueError("weight must be >= 1")


@dataclass(frozen=True)
class Committee:
    #: Epoch this committee is authorized to sign headers for.
    epoch: int
    members: tuple[Member, ...]
    #: Absolute weight threshold; a certificate needs >= this.
    quorum_weight: int

    def __post_init__(self) -> None:
        if not isinstance(self.epoch, int) or isinstance(self.epoch, bool):
            raise TypeError("epoch must be an int")
        if self.epoch < 0:
            raise ValueError("epoch must be >= 0")
        if not self.members:
            raise ValueError("committee must have members")
        if any(not isinstance(m, Member) for m in self.members):
            raise TypeError("members must be Member instances")
        if self.quorum_weight < 1:
            raise ValueError("quorum_weight must be >= 1")

    @property
    def total_weight(self) -> int:
        return sum(m.weight for m in self.members)

    def member_by_key(self, public_key: bytes) -> Member | None:
        for m in self.members:
            if m.public_key == public_key:
                return m
        return None


@dataclass(frozen=True)
class Header:
    chain_id: str
    height: int
    round: int
    epoch: int
    timestamp: int
    parent_digest: bytes  # 32 bytes; b"\\x00" * 32 for a genesis header
    payload_root: bytes  # 32 bytes; synthetic body commitment
    #: Committees are announced one epoch in advance. Populated only on the
    #: last header of an epoch in this simplified protocol; otherwise None.
    next_committee: Committee | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.chain_id, str) or not self.chain_id:
            raise ValueError("chain_id required")
        for name, value in (
            ("height", self.height),
            ("round", self.round),
            ("epoch", self.epoch),
            ("timestamp", self.timestamp),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a non-negative int")
        for name, value in (
            ("parent_digest", self.parent_digest),
            ("payload_root", self.payload_root),
        ):
            if not isinstance(value, (bytes, bytearray)) or len(value) != 32:
                raise ValueError(f"{name} must be 32 bytes")
        if self.next_committee is not None and not isinstance(
            self.next_committee, Committee
        ):
            raise TypeError("next_committee must be a Committee or None")


GENESIS_PARENT = b"\x00" * 32


@dataclass(frozen=True)
class Vote:
    #: 32-byte signer public key.
    signer: bytes
    #: Ed25519 signature over the certificate message (see crypto.py).
    signature: bytes

    def __post_init__(self) -> None:
        if len(self.signer) != 32:
            raise ValueError("vote.signer must be 32 bytes")
        if len(self.signature) != 64:
            raise ValueError("vote.signature must be 64 bytes")


@dataclass(frozen=True)
class Certificate:
    #: Digest of the single header this certificate authorizes.
    header_digest: bytes
    votes: tuple[Vote, ...]

    def __post_init__(self) -> None:
        if len(self.header_digest) != 32:
            raise ValueError("certificate.header_digest must be 32 bytes")
        if not self.votes:
            raise ValueError("certificate requires at least one vote")
        if any(not isinstance(v, Vote) for v in self.votes):
            raise TypeError("votes must be Vote instances")


@dataclass(frozen=True)
class Checkpoint:
    chain_id: str
    header: Header
    committee: Committee
    #: Trust period (seconds) that applies *after* this checkpoint.
    trust_period_seconds: int

    def __post_init__(self) -> None:
        if not isinstance(self.header, Header):
            raise TypeError("checkpoint.header must be a Header")
        if not isinstance(self.committee, Committee):
            raise TypeError("checkpoint.committee must be a Committee")
        if self.trust_period_seconds < 1:
            raise ValueError("trust_period_seconds must be >= 1")
        if self.header.chain_id != self.chain_id:
            raise ValueError("checkpoint chain_id/header mismatch")
        if self.committee.epoch != self.header.epoch:
            raise ValueError("checkpoint committee epoch must equal header epoch")


@dataclass(frozen=True)
class CheckpointEnvelope:
    """A checkpoint signed out-of-band by the configured checkpoint key."""

    checkpoint: Checkpoint
    signature: bytes

    def __post_init__(self) -> None:
        if len(self.signature) != 64:
            raise ValueError("checkpoint signature must be 64 bytes")
