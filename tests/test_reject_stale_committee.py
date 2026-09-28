"""Rejection: old committee signing a new-era header."""

from __future__ import annotations

import pytest

from lightclient import codec
from lightclient.errors import ErrorCode
from lightclient.fixtures.builder import build_header


def _tip_digest(kernel):
    return kernel.tip().digest


def test_rotation_requires_announced_committee(bootstrapped):
    kernel, builder = bootstrapped
    before = _tip_digest(kernel)
    # epoch 1 header with valid c0 signatures but no announcement existed
    blk = builder.add_block(
        signer_labels=["c0-a", "c0-b"],
        epoch=1,
        committee_for_signing=builder.committees[0],
    )
    with pytest.raises(Exception) as ei:
        kernel.apply_header(blk.header, blk.certificate)
    assert ei.value.code == ErrorCode.COMMITTEE_UNKNOWN
    assert "no committee was announced" in ei.value.reason
    assert _tip_digest(kernel) == before


def test_old_committee_signing_new_header_is_stale(bootstrapped):
    kernel, builder = bootstrapped
    c1 = builder.add_committee(1, [("c1-a", 1), ("c1-b", 1), ("c1-c", 1)])
    announce = builder.add_block(
        signer_labels=["c0-a", "c0-b"], next_committee=c1.committee
    )
    kernel.apply_header(announce.header, announce.certificate)
    first_new = builder.add_block(signer_labels=["c1-a", "c1-b"], epoch=1)
    kernel.apply_header(first_new.header, first_new.certificate)

    before = _tip_digest(kernel)
    # next header is epoch 1, but signed by the superseded epoch-0 keys
    stale = builder.add_block(
        signer_labels=["c0-a", "c0-b"],
        epoch=1,
        committee_for_signing=builder.committees[0],
    )
    with pytest.raises(Exception) as ei:
        kernel.apply_header(stale.header, stale.certificate)
    assert ei.value.code == ErrorCode.STALE_COMMITTEE
    assert ei.value.category.value == "state"
    assert ei.value.detail["signer_epoch"] == 0
    assert ei.value.detail["required_epoch"] == 1
    assert _tip_digest(kernel) == before
    # the rejected stale block must not have advanced the chain: build the
    # continuation explicitly off the still-trusted tip
    good = builder.add_block(
        signer_labels=["c1-b", "c1-c"],
        epoch=1,
        height=3,
        round=4,
        timestamp=first_new.header.timestamp + 10,
        parent_digest=_tip_digest(kernel),
    )
    result = kernel.apply_header(good.header, good.certificate)
    assert result.decision == "accepted"


def test_epoch_skips_are_unknown_not_stale(bootstrapped):
    kernel, builder = bootstrapped
    from lightclient.types import Certificate, Vote
    from lightclient import codec

    before = _tip_digest(kernel)
    h = build_header(
        chain_id=builder.chain_id,
        height=1,
        round=1,
        epoch=5,
        timestamp=1_000_010,
        parent_digest=builder.tip_digest,
    )
    # a structurally valid cert from random keys (membership will not matter;
    # epoch rule fires first)
    cert = Certificate(
        header_digest=codec.header_digest(h),
        votes=(Vote(signer=b"\x03" * 32, signature=b"\x04" * 64),),
    )
    with pytest.raises(Exception) as ei:
        kernel.apply_header(h, cert)
    assert ei.value.code == ErrorCode.COMMITTEE_UNKNOWN
    assert "one epoch at a time" in ei.value.reason
    assert _tip_digest(kernel) == before


def test_bad_announcement_epoch_rejected(bootstrapped):
    kernel, builder = bootstrapped
    before = _tip_digest(kernel)
    # announced committee claims epoch 9 instead of epoch+1
    bad = builder.add_committee(9, [("z1", 1), ("z2", 1)])
    blk = builder.add_block(
        signer_labels=["c0-a", "c0-b"], next_committee=bad.committee
    )
    with pytest.raises(Exception) as ei:
        kernel.apply_header(blk.header, blk.certificate)
    assert ei.value.code == ErrorCode.COMMITTEE_BAD_TRANSITION
    assert ei.value.detail["announced_epoch"] == 9
    assert _tip_digest(kernel) == before


def test_announced_committee_with_unreachable_quorum_rejected(bootstrapped):
    kernel, builder = bootstrapped
    before = _tip_digest(kernel)
    # unreachable quorum: total weight below the configured policy quorum
    from lightclient.fixtures.builder import build_committee

    weak2 = build_committee(1, [("w1", 1)], 2)
    blk2 = builder.add_block(
        signer_labels=["c0-a", "c0-b"], next_committee=weak2.committee
    )
    with pytest.raises(Exception) as ri:
        kernel.apply_header(blk2.header, blk2.certificate)
    assert ri.value.code == ErrorCode.COMMITTEE_BAD_TRANSITION
    assert ri.value.detail["total_weight"] == 1
    assert _tip_digest(kernel) == before


def test_committee_change_is_authorized_only_by_previous_committee(bootstrapped):
    """A rotation announcement riding on a certificate that fails quorum is
    never installed, even though the header is otherwise well-formed."""
    kernel, builder = bootstrapped
    before = _tip_digest(kernel)
    c1 = builder.add_committee(1, [("c1-a", 1), ("c1-b", 1), ("c1-c", 1)])
    # header announces c1 but only one weight -> below quorum: the change is
    # not authorized and must not be stored.
    announce = builder.add_block(
        signer_labels=["c0-a"], next_committee=c1.committee
    )
    with pytest.raises(Exception) as ei:
        kernel.apply_header(announce.header, announce.certificate)
    assert ei.value.code == ErrorCode.WEIGHT_BELOW_QUORUM
    cid = codec.committee_id(c1.committee)
    assert not kernel.store.has_committee(cid)
    assert _tip_digest(kernel) == before
