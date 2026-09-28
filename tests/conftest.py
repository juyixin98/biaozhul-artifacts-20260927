"""Shared synthetic fixtures.

All keys are generated locally (no external accounts). Test reference answers
(weights, roots, verdicts) are computed in the test layer itself; the
``independent`` package contains a second, fully separate implementation used
to cross-check the detector's evidence packets.
"""

from __future__ import annotations

import pytest

from ffg_slash.crypto import derive_seed, keypair_from_seed, sign_vote
from ffg_slash.logging_setup import configure_logging
from ffg_slash.models import Vote
from ffg_slash.registry import ValidatorRegistry
from ffg_slash.replay import build_service
from ffg_slash.storage import Storage

CHAIN_ID = 4242
GENESIS_ROOT = b"\x11" * 32
LABELS = ["alpha", "bravo", "charlie", "delta", "echo"]


def make_key(label: str) -> tuple[bytes, bytes]:
    """Return (seed, pubkey) deterministically derived from a label."""
    return keypair_from_seed(derive_seed(label))


def make_vote(seed: bytes, pubkey: bytes, *, chain_id: int = CHAIN_ID,
              source_epoch: int, target_epoch: int,
              source_root: bytes | None = None,
              target_root: bytes | None = None) -> Vote:
    """Construct a properly signed vote with deterministic default roots."""
    source_root = source_root or (bytes([source_epoch & 0xFF]) * 32)
    target_root = target_root or (bytes([target_epoch & 0xFF]) * 32)
    sig = sign_vote(seed, chain_id=chain_id, validator_pubkey=pubkey,
                    source_epoch=source_epoch, source_root=source_root,
                    target_epoch=target_epoch, target_root=target_root)
    return Vote(chain_id=chain_id, validator_pubkey=pubkey,
                source_epoch=source_epoch, source_root=source_root,
                target_epoch=target_epoch, target_root=target_root,
                signature=sig)


def corrupt_signature(vote: Vote) -> Vote:
    """Flip one byte of the signature -> must fail verification."""
    bad = bytes([vote.signature[0] ^ 0x01]) + vote.signature[1:]
    return Vote(chain_id=vote.chain_id, validator_pubkey=vote.validator_pubkey,
                source_epoch=vote.source_epoch, source_root=vote.source_root,
                target_epoch=vote.target_epoch, target_root=vote.target_root,
                signature=bad)


@pytest.fixture
def keys():
    return {label: make_key(label) for label in LABELS}


@pytest.fixture
def service_factory(keys, tmp_path):
    created = []

    def _factory(epochs_members: dict[int, list[str]],
                 weights: dict[str, int] | None = None,
                 chain_id: int = CHAIN_ID):
        weights = weights or {}
        log_dir = tmp_path / f"logs-{len(created)}"
        logger, run_id, _ = configure_logging(log_dir)
        storage = Storage(tmp_path / f"test-{len(created)}.sqlite3")
        storage.init_meta(chain_id, GENESIS_ROOT)
        registry = ValidatorRegistry(chain_id)
        for epoch, labels in epochs_members.items():
            members = {keys[label][1]: weights.get(label, 1) for label in labels}
            registry.add_epoch(epoch, members)
            storage.upsert_epoch(epoch, members)
        from ffg_slash.detector import SlashingService
        service = SlashingService(
            chain_id=chain_id, genesis_root=GENESIS_ROOT,
            registry=registry, storage=storage, run_id=run_id, logger=logger)
        created.append(service)
        return service

    return _factory


@pytest.fixture
def standard_service(service_factory, keys):
    """4 validators weight 1 in epochs 1,2; epoch 3 rotates (delta out, echo in).

    Epochs 4/5 keep the rotated set so nesting spanning the change is testable.
    """
    return service_factory({
        1: ["alpha", "bravo", "charlie", "delta"],
        2: ["alpha", "bravo", "charlie", "delta"],
        3: ["alpha", "bravo", "charlie", "echo"],
        4: ["alpha", "bravo", "charlie", "echo"],
        5: ["alpha", "bravo", "charlie", "echo"],
    })
