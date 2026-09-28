"""Independent fixture construction for the local test chain.

Determinism: every private key is ``SHA256(label)`` interpreted as an
Ed25519 seed. Two runs with the same labels produce identical signatures,
which lets us commit golden vectors and replay problems byte-for-byte.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import codec
from ..crypto import sign_checkpoint, sign_header
from ..types import (
    Certificate,
    Checkpoint,
    CheckpointEnvelope,
    Committee,
    GENESIS_PARENT,
    Header,
    Member,
    Vote,
)


# --------------------------------------------------------------- keys

def build_key(label: str) -> tuple[bytes, bytes]:
    """Deterministic ``(private_seed, public_key)`` from a string label."""
    seed = hashlib.sha256(b"lc-fixture-key|" + label.encode("utf-8")).digest()
    # cryptography derives the public key from the 32-byte seed directly.
    from cryptography.hazmat.primitives.asymmetric import ed25519

    sk = ed25519.Ed25519PrivateKey.from_private_bytes(seed)
    return seed, sk.public_key().public_bytes_raw()


def member_seed(label: str) -> bytes:
    return build_key(label)[0]


# ---------------------------------------------------------- committees

@dataclass
class CommitteeSecrets:
    committee: Committee
    seeds: dict[bytes, bytes] = field(default_factory=dict)  # public -> seed
    labels: dict[str, bytes] = field(default_factory=dict)  # label -> public

    def seed_for(self, label: str) -> bytes:
        return self.seeds[self.labels[label]]

    def public_for(self, label: str) -> bytes:
        return self.labels[label]


def build_committee(
    epoch: int, members: list[tuple[str, int]], quorum_weight: int
) -> CommitteeSecrets:
    """``members`` is a list of ``(label, weight)``; keys are derived."""
    seeds: dict[bytes, bytes] = {}
    labels: dict[str, bytes] = {}
    member_objs: list[Member] = []
    for label, weight in members:
        seed, pub = build_key(label)
        member_objs.append(Member(public_key=pub, weight=weight))
        seeds[pub] = seed
        labels[label] = pub
    return CommitteeSecrets(
        committee=Committee(
            epoch=epoch,
            members=tuple(member_objs),
            quorum_weight=quorum_weight,
        ),
        seeds=seeds,
        labels=labels,
    )


# ------------------------------------------------------------- headers

def build_header(
    *,
    chain_id: str,
    height: int,
    round: int,
    epoch: int,
    timestamp: int,
    parent_digest: bytes,
    payload: bytes | None = None,
    next_committee: Committee | None = None,
) -> Header:
    if payload is None:
        payload = hashlib.sha256(
            b"lc-fixture-payload|" + height.to_bytes(8, "big") + round.to_bytes(8, "big")
        ).digest()
    return Header(
        chain_id=chain_id,
        height=height,
        round=round,
        epoch=epoch,
        timestamp=timestamp,
        parent_digest=parent_digest,
        payload_root=payload,
        next_committee=next_committee,
    )


def build_certificate(header: Header, signer_seeds: list[bytes]) -> Certificate:
    votes: list[Vote] = []
    for seed in signer_seeds:
        from cryptography.hazmat.primitives.asymmetric import ed25519

        pub = (
            ed25519.Ed25519PrivateKey.from_private_bytes(seed)
            .public_key()
            .public_bytes_raw()
        )
        votes.append(Vote(signer=pub, signature=sign_header(seed, header)))
    return Certificate(header_digest=codec.header_digest(header), votes=tuple(votes))


def build_checkpoint_envelope(
    *,
    chain_id: str,
    header: Header,
    committee: Committee,
    trust_period_seconds: int,
    checkpoint_seed: bytes,
) -> CheckpointEnvelope:
    cp = Checkpoint(
        chain_id=chain_id,
        header=header,
        committee=committee,
        trust_period_seconds=trust_period_seconds,
    )
    sig = sign_checkpoint(checkpoint_seed, cp)
    return CheckpointEnvelope(checkpoint=cp, signature=sig)


# ------------------------------------------------------- chain builder

@dataclass
class BuiltBlock:
    header: Header
    certificate: Certificate
    signer_labels: list[str]
    signed_weight: int


class ChainBuilder:
    """Builds a legitimate, continuously-connected header chain.

    The builder knows only encoding + signing. It does not validate against
    a kernel; tests decide which blocks to feed (including tampered ones).
    """

    def __init__(
        self,
        *,
        chain_id: str = "local-test-chain-0001",
        trust_period_seconds: int = 3600,
        quorum_weight: int = 2,
        genesis_members: list[tuple[str, int]] | None = None,
    ) -> None:
        self.chain_id = chain_id
        self.trust_period_seconds = trust_period_seconds
        self.quorum_weight = quorum_weight
        self.checkpoint_seed, self.checkpoint_pub = build_key("trusted-checkpoint")
        if genesis_members is None:
            genesis_members = [("c0-a", 1), ("c0-b", 1), ("c0-c", 1)]
        self.genesis_secrets = build_committee(0, genesis_members, quorum_weight)
        self.committees: dict[int, CommitteeSecrets] = {0: self.genesis_secrets}
        self.blocks: list[BuiltBlock] = []
        self.current_epoch = 0
        self._tip_digest = GENESIS_PARENT
        self._tip_height = -1
        self._tip_round = -1
        self._tip_ts = 0

    # --------------------------------------------------------- genesis

    def genesis(
        self,
        *,
        height: int = 0,
        round: int = 0,
        epoch: int = 0,
        timestamp: int = 1_000_000,
    ) -> tuple[Header, CheckpointEnvelope]:
        committee = self.genesis_secrets.committee
        g = build_header(
            chain_id=self.chain_id,
            height=height,
            round=round,
            epoch=epoch,
            timestamp=timestamp,
            parent_digest=GENESIS_PARENT,
        )
        env = build_checkpoint_envelope(
            chain_id=self.chain_id,
            header=g,
            committee=committee,
            trust_period_seconds=self.trust_period_seconds,
            checkpoint_seed=self.checkpoint_seed,
        )
        self._tip_digest = codec.header_digest(g)
        self._tip_height = height
        self._tip_round = round
        self._tip_ts = timestamp
        return g, env

    def add_committee(
        self, epoch: int, members: list[tuple[str, int]]
    ) -> CommitteeSecrets:
        sec = build_committee(epoch, members, self.quorum_weight)
        self.committees[epoch] = sec
        return sec

    def add_block(
        self,
        *,
        signer_labels: list[str],
        epoch: int | None = None,
        timestamp_step: int = 10,
        round_step: int = 1,
        next_committee: Committee | None = None,
        payload: bytes | None = None,
        height: int | None = None,
        round: int | None = None,
        timestamp: int | None = None,
        parent_digest: bytes | None = None,
        committee_for_signing: CommitteeSecrets | None = None,
        certificate: Certificate | None = None,
    ) -> BuiltBlock:
        """Append a properly-connected block signed by the named members.

        ``height``/``round``/``timestamp``/``parent_digest`` overrides exist
        so tests can deliberately construct boundary or malformed inputs.
        ``certificate`` bypasses signing entirely (for invalid-cert tests).
        """
        if epoch is None:
            epoch = self.current_epoch
        h = height if height is not None else self._tip_height + 1
        r = round if round is not None else self._tip_round + round_step
        ts = timestamp if timestamp is not None else self._tip_ts + timestamp_step
        parent = parent_digest if parent_digest is not None else self._tip_digest
        header = build_header(
            chain_id=self.chain_id,
            height=h,
            round=r,
            epoch=epoch,
            timestamp=ts,
            parent_digest=parent,
            payload=payload,
            next_committee=next_committee,
        )
        sec = committee_for_signing
        if sec is None:
            sec = self.committees.get(epoch)
        if sec is None:
            # Signing committee may be unknown (used to build COMMITTEE_UNKNOWN
            # inputs); allow empty seeds and let the test add votes itself.
            seeds = []
        else:
            seeds = [sec.seed_for(label) for label in signer_labels]
        cert = certificate if certificate is not None else build_certificate(header, seeds)
        signed_weight = (
            sum(
                sec.committee.member_by_key(sec.public_for(l)).weight
                for l in signer_labels
            )
            if sec is not None
            else 0
        )
        block = BuiltBlock(
            header=header,
            certificate=cert,
            signer_labels=list(signer_labels),
            signed_weight=signed_weight,
        )
        # Only advance the builder cursor for a canonical append.
        if (
            height is None
            and round is None
            and timestamp is None
            and parent_digest is None
        ):
            self._tip_digest = codec.header_digest(header)
            self._tip_height = h
            self._tip_round = r
            self._tip_ts = ts
            self.current_epoch = header.epoch
        self.blocks.append(block)
        return block

    @property
    def tip_digest(self) -> bytes:
        return self._tip_digest

    def rewind_cursor(self, height: int, round: int, timestamp: int, digest: bytes) -> None:
        """Point the builder cursor at an earlier block (to build forks)."""
        self._tip_height = height
        self._tip_round = round
        self._tip_ts = timestamp
        self._tip_digest = digest


# ------------------------------------------------------ replay export

def build_replay_file(
    *,
    chain_id: str,
    envelope: CheckpointEnvelope,
    blocks: list[BuiltBlock],
) -> dict[str, Any]:
    return {
        "format": "local-header-lightclient-replay/v1",
        "chain_id": chain_id,
        "checkpoint_envelope": codec.encode_envelope(envelope).hex(),
        "items": [
            {
                "source": f"block-{i}",
                "header": codec.encode_header(b.header).hex(),
                "certificate": codec.encode_certificate(b.certificate).hex(),
            }
            for i, b in enumerate(blocks)
        ],
    }


def write_replay_file(path: str | Path, doc: dict[str, Any]) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, indent=2, sort_keys=True))
    return p


def load_replay_file(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text())


# alias matching package export
write_golden_fixture = write_replay_file


# Convenience: a fully valid chain with one committee rotation.
def build_legitimate_chain(
    *, n: int = 5, rotate_at: int = 3
) -> tuple[ChainBuilder, Header, CheckpointEnvelope, list[BuiltBlock]]:
    builder = ChainBuilder()
    genesis_h, env = builder.genesis()
    c1 = builder.add_committee(1, [("c1-a", 1), ("c1-b", 1), ("c1-c", 1)])
    blocks: list[BuiltBlock] = []
    for i in range(1, n + 1):
        if i == rotate_at:
            block = builder.add_block(
                signer_labels=["c0-a", "c0-b"], next_committee=c1.committee
            )
        elif i > rotate_at:
            block = builder.add_block(signer_labels=["c1-a", "c1-b"], epoch=1)
        else:
            block = builder.add_block(signer_labels=["c0-a", "c0-b"])
        blocks.append(block)
    return builder, genesis_h, env, blocks
