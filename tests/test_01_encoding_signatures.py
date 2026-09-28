"""Encoding determinism and signature domain binding."""

from __future__ import annotations

import pytest

from ffg_slash.crypto import sign_vote, verify_vote
from ffg_slash.encoding import (
    EncodingError,
    signing_preimage,
    snapshot_root,
    vote_message_root,
)

from .conftest import CHAIN_ID, make_key, make_vote


def test_vote_body_is_deterministic_fixed_width(keys):
    alpha = keys["alpha"]
    v = make_vote(alpha[0], alpha[1], source_epoch=2, target_epoch=3)
    pre = signing_preimage(
        chain_id=CHAIN_ID, validator_pubkey=alpha[1],
        source_epoch=2, source_root=v.source_root,
        target_epoch=3, target_root=v.target_root)
    assert len(pre) == 152  # 32 domain + 8 chain + 32 pubkey + 80 body
    # same inputs -> same preimage (no randomness)
    assert pre == signing_preimage(
        chain_id=CHAIN_ID, validator_pubkey=alpha[1],
        source_epoch=2, source_root=v.source_root,
        target_epoch=3, target_root=v.target_root)


def test_signature_binds_each_domain_component(keys):
    alpha = keys["alpha"][1]
    seed = keys["alpha"][0]
    kwargs = dict(validator_pubkey=alpha, source_epoch=2,
                  source_root=b"\x02" * 32, target_epoch=3,
                  target_root=b"\x03" * 32)
    sig = sign_vote(seed, chain_id=CHAIN_ID, **kwargs)

    # correct message verifies
    assert verify_vote(alpha, sig, chain_id=CHAIN_ID, **kwargs) is True

    # changing ANY bound component invalidates the signature
    assert verify_vote(alpha, sig, chain_id=CHAIN_ID + 1, **kwargs) is False
    assert verify_vote(keys["bravo"][1], sig, chain_id=CHAIN_ID, **kwargs) is False
    assert verify_vote(alpha, sig, chain_id=CHAIN_ID,
                       validator_pubkey=alpha, source_epoch=1,
                       source_root=kwargs["source_root"], target_epoch=3,
                       target_root=kwargs["target_root"]) is False
    assert verify_vote(alpha, sig, chain_id=CHAIN_ID,
                       validator_pubkey=alpha, source_epoch=2,
                       source_root=b"\x09" * 32, target_epoch=3,
                       target_root=kwargs["target_root"]) is False
    assert verify_vote(alpha, sig, chain_id=CHAIN_ID,
                       validator_pubkey=alpha, source_epoch=2,
                       source_root=kwargs["source_root"], target_epoch=4,
                       target_root=kwargs["target_root"]) is False
    assert verify_vote(alpha, sig, chain_id=CHAIN_ID,
                       validator_pubkey=alpha, source_epoch=2,
                       source_root=kwargs["source_root"], target_epoch=3,
                       target_root=b"\x0a" * 32) is False


def test_message_root_distinguishes_content_not_just_target(keys):
    alpha = keys["alpha"]
    v1 = make_vote(alpha[0], alpha[1], source_epoch=2, target_epoch=3,
                   target_root=b"\xAA" * 32)
    v2 = make_vote(alpha[0], alpha[1], source_epoch=2, target_epoch=3,
                   target_root=b"\xBB" * 32)
    r1 = vote_message_root(source_epoch=2, source_root=v1.source_root,
                           target_epoch=3, target_root=v1.target_root)
    r2 = vote_message_root(source_epoch=2, source_root=v2.source_root,
                           target_epoch=3, target_root=v2.target_root)
    assert r1 != r2


def test_snapshot_root_is_order_independent_and_weight_sensitive(keys):
    a, b = keys["alpha"][1], keys["bravo"][1]
    r1 = snapshot_root(epoch=2, chain_id=CHAIN_ID, members=[(a, 1), (b, 2)])
    r2 = snapshot_root(epoch=2, chain_id=CHAIN_ID, members=[(b, 2), (a, 1)])
    assert r1 == r2
    r3 = snapshot_root(epoch=2, chain_id=CHAIN_ID, members=[(a, 1), (b, 3)])
    assert r1 != r3
    r4 = snapshot_root(epoch=3, chain_id=CHAIN_ID, members=[(a, 1), (b, 2)])
    assert r1 != r4


def test_encoding_rejects_bad_types_and_ranges():
    with pytest.raises(EncodingError):
        signing_preimage(chain_id="4242", validator_pubkey=b"\x00" * 32,
                         source_epoch=1, source_root=b"\x00" * 32,
                         target_epoch=2, target_root=b"\x00" * 32)
    with pytest.raises(EncodingError):
        signing_preimage(chain_id=0, validator_pubkey=b"short",
                         source_epoch=1, source_root=b"\x00" * 32,
                         target_epoch=2, target_root=b"\x00" * 32)
    with pytest.raises(EncodingError):
        signing_preimage(chain_id=0, validator_pubkey=b"\x00" * 32,
                         source_epoch=-1, source_root=b"\x00" * 32,
                         target_epoch=2, target_root=b"\x00" * 32)
