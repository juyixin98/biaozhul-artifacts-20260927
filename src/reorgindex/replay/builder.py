"""Synthetic chain builder used to generate reviewable fixtures.

Everything here is local: Ed25519 keys are generated in-process, proof-of-work
searches an ASCII nonce counter against the documented, low difficulty, and
blocks are canonical-JSON signed by a single authorized fixture producer.

Forks need independent account-nonce state, so each :class:`BranchBuilder`
carries its own nonce counter and can be snapshotted at a fork point.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ..crypto.hashing import ZERO_HASH, block_identity_hash, pow_satisfied
from ..crypto.keys import address_from_private_key, generate_private_key
from ..kernel.models import make_block, make_mint, make_transfer


def mine(
    *,
    height: int,
    parent: str,
    producer: Ed25519PrivateKey,
    transactions: list[dict],
    difficulty: int,
    timestamp: str,
    start_nonce: int = 0,
) -> tuple[dict, str]:
    """Search a nonce satisfying PoW; return (sealed block, identity hash)."""
    nonce = start_nonce
    while True:
        candidate = make_block(
            height=height,
            parent=parent,
            producer=producer,
            transactions=transactions,
            difficulty=difficulty,
            timestamp=timestamp,
            nonce=nonce,
        )
        identity = block_identity_hash(candidate)
        if pow_satisfied(identity, difficulty):
            return candidate, identity
        nonce += 1


@dataclass
class FixtureKeys:
    producer: Ed25519PrivateKey
    users: dict[str, Ed25519PrivateKey]
    addresses: dict[str, str]

    @classmethod
    def create(cls) -> "FixtureKeys":
        producer = generate_private_key()
        users = {name: generate_private_key() for name in ("alice", "bob", "carol")}
        addresses = {"producer": address_from_private_key(producer)}
        for name, key in users.items():
            addresses[name] = address_from_private_key(key)
        return cls(producer=producer, users=users, addresses=addresses)


@dataclass
class BranchBuilder:
    """Builds blocks along one chain path with its own account-nonce state."""

    keys: FixtureKeys
    difficulty: int = 16
    tip_name: Optional[str] = None
    tip_hash: str = ZERO_HASH
    tip_height: int = -1
    nonces: dict[str, int] = field(default_factory=dict)
    blocks: dict[str, dict] = field(default_factory=dict)
    hashes: dict[str, str] = field(default_factory=dict)
    heights: dict[str, int] = field(default_factory=dict)

    def snapshot(self, *, tip_name: str) -> "BranchBuilder":
        clone = BranchBuilder(
            keys=self.keys,
            difficulty=self.difficulty,
            tip_name=tip_name,
            tip_hash=self.hashes[tip_name],
            tip_height=self.heights[tip_name],
            nonces=dict(self.nonces),   # independent copy: forks evolve separately
            blocks=self.blocks,         # shared block dictionary
            hashes=self.hashes,
            heights=self.heights,
        )
        return clone

    @property
    def addresses(self) -> dict[str, str]:
        return self.keys.addresses

    def _resolve(self, name_or_address: str) -> str:
        return self.keys.addresses.get(name_or_address, name_or_address)

    def genesis(
        self,
        *,
        name: str = "g0",
        mints: Optional[dict[str, int]] = None,
        timestamp: str = "2026-09-27T00:00:00+00:00",
        difficulty: Optional[int] = None,
    ) -> tuple[dict, str]:
        mints = mints or {"alice": 1_000, "bob": 500}
        txs = [
            make_mint(
                signer=self.keys.users[user_name],
                recipient=self.addresses[user_name],
                amount=amount,
            )
            for user_name, amount in mints.items()
        ]
        block, identity = mine(
            height=0,
            parent=ZERO_HASH,
            producer=self.keys.producer,
            transactions=txs,
            difficulty=self.difficulty if difficulty is None else difficulty,
            timestamp=timestamp,
        )
        self._register(name, block, identity, 0)
        return block, identity

    def _register(self, name: str, block: dict, identity: str, height: int) -> None:
        self.blocks[name] = block
        self.hashes[name] = identity
        self.heights[name] = height
        self.tip_name = name
        self.tip_hash = identity
        self.tip_height = height

    def transfer(
        self,
        *,
        sender: str,
        recipient: str,
        amount: int,
        fee: int = 0,
    ) -> dict:
        sender_address = self._resolve(sender)
        next_nonce = self.nonces.get(sender_address, 0) + 1
        self.nonces[sender_address] = next_nonce
        return make_transfer(
            signer=self.keys.users[sender],
            nonce=next_nonce,
            recipient=self._resolve(recipient),
            amount=amount,
            fee=fee,
            fee_recipient=self.addresses["producer"],
        )

    def child(
        self,
        *,
        name: str,
        transfers: Optional[list[dict]] = None,
        timestamp: Optional[str] = None,
        start_nonce: int = 0,
        difficulty: Optional[int] = None,
    ) -> tuple[dict, str]:
        if self.tip_name is None:
            raise ValueError("call genesis() before child()")
        txs = [self.transfer(**t) for t in (transfers or [])]
        if not txs:
            raise ValueError("blocks must contain at least one transaction")
        height = self.tip_height + 1
        ts = timestamp or f"2026-09-27T00:{height:02d}:00+00:00"
        block, identity = mine(
            height=height,
            parent=self.tip_hash,
            producer=self.keys.producer,
            transactions=txs,
            difficulty=self.difficulty if difficulty is None else difficulty,
            timestamp=ts,
            start_nonce=start_nonce,
        )
        self._register(name, block, identity, height)
        return block, identity

    def child_with_txs(
        self,
        *,
        name: str,
        transactions: list[dict],
        timestamp: Optional[str] = None,
        start_nonce: int = 0,
        difficulty: Optional[int] = None,
    ) -> tuple[dict, str]:
        """Mine a child from an explicit transaction list (cross-fork dup)."""
        if self.tip_name is None:
            raise ValueError("call genesis() before child()")
        if not transactions:
            raise ValueError("blocks must contain at least one transaction")
        height = self.tip_height + 1
        # Keep this branch's nonce counter consistent with explicitly
        # supplied transactions (e.g. a duplicate cross-fork transaction).
        for tx in transactions:
            if tx["type"] == "transfer":
                prev = self.nonces.get(tx["sender"], 0)
                self.nonces[tx["sender"]] = max(prev, int(tx["nonce"]))
        ts = timestamp or f"2026-09-27T00:{height:02d}:00+00:00"
        block, identity = mine(
            height=height,
            parent=self.tip_hash,
            producer=self.keys.producer,
            transactions=transactions,
            difficulty=self.difficulty if difficulty is None else difficulty,
            timestamp=ts,
            start_nonce=start_nonce,
        )
        self._register(name, block, identity, height)
        return block, identity

    def make_transfer(
        self,
        *,
        sender: str,
        recipient: str,
        amount: int,
        nonce: int,
        fee: int = 0,
    ) -> dict:
        return make_transfer(
            signer=self.keys.users[sender],
            nonce=nonce,
            recipient=self._resolve(recipient),
            amount=amount,
            fee=fee,
            fee_recipient=self.addresses["producer"],
        )
