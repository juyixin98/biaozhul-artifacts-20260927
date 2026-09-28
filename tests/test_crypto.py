"""Crypto layer tests: threshold accumulation, duplicate counting, tampering."""

from __future__ import annotations

import pytest

from lightclient import codec
from lightclient.crypto import verify_certificate
from lightclient.errors import ErrorCode
from lightclient.fixtures.builder import (
    ChainBuilder,
    build_certificate,
    build_header,
)
from lightclient.types import Vote


def _signed_block(builder, labels, header_kw=None):
    kw = dict(
        chain_id=builder.chain_id, height=1, round=1, epoch=0,
        timestamp=1_000_010, parent_digest=codec.header_digest(
            builder.genesis()[0]
        ),
    )
    if header_kw:
        kw.update(header_kw)
    header = build_header(**kw)
    cert = build_certificate(
        header, [builder.genesis_secrets.seed_for(l) for l in labels]
    )
    return header, cert


def test_quorum_exact_threshold_accepted():
    b = ChainBuilder()
    b.genesis()
    header, cert = _signed_block(b, ["c0-a", "c0-b"])  # weights 1+1 == 2
    eval_ = verify_certificate(header, cert, b.genesis_secrets.committee)
    assert eval_.signed_weight == 2
    assert eval_.has_quorum is True


def test_weight_below_threshold_rejected_with_detail():
    b = ChainBuilder()
    b.genesis()
    header, cert = _signed_block(b, ["c0-a"])  # weight 1 < 2
    with pytest.raises(Exception) as ei:
        verify_certificate(header, cert, b.genesis_secrets.committee)
    assert ei.value.code == ErrorCode.WEIGHT_BELOW_QUORUM
    assert ei.value.detail["signed_weight"] == 1
    assert ei.value.detail["quorum_weight"] == 2
    assert ei.value.detail["participant_count"] == 1


def test_weighted_member_uses_weight_not_headcount():
    # committee [w2=2, w2b=2, w1=1], quorum 2: the single weight-2 member
    # alone reaches quorum even though it is only one participant.
    b = ChainBuilder(genesis_members=[("w2", 2), ("w2b", 2), ("w1", 1)])
    b.genesis()
    header, cert = _signed_block(b, ["w2"])
    eval_ = verify_certificate(header, cert, b.genesis_secrets.committee)
    assert eval_.participant_count == 1
    assert eval_.signed_weight == 2
    assert eval_.has_quorum is True


def test_duplicate_signer_counts_once_and_is_rejected():
    b = ChainBuilder()
    b.genesis()
    header, cert = _signed_block(b, ["c0-a"])
    v = cert.votes[0]
    dup_cert = cert.__class__(
        header_digest=cert.header_digest,
        votes=(
            Vote(signer=v.signer, signature=v.signature),
            Vote(signer=v.signer, signature=v.signature),
        ),
    )
    with pytest.raises(Exception) as ei:
        verify_certificate(header, dup_cert, b.genesis_secrets.committee)
    assert ei.value.code == ErrorCode.SIGNATURE_INVALID
    assert "duplicate signer" in ei.value.reason


def test_signer_not_on_committee_rejected():
    b = ChainBuilder()
    b.genesis()
    # A random key that no committee member holds.
    from lightclient.crypto import generate_keypair

    foreign_seed, foreign_pub = generate_keypair()
    header = build_header(
        chain_id=b.chain_id, height=1, round=1, epoch=0,
        timestamp=1_000_010,
        parent_digest=codec.header_digest(b.genesis()[0]),
    )
    cert = build_certificate(header, [foreign_seed])
    with pytest.raises(Exception) as ei:
        verify_certificate(header, cert, b.genesis_secrets.committee)
    assert ei.value.code == ErrorCode.SIGNATURE_INVALID
    assert "not on the authorizing committee" in ei.value.reason


def test_tampered_signature_rejected():
    b = ChainBuilder()
    b.genesis()
    header, cert = _signed_block(b, ["c0-a", "c0-b"])
    v0 = cert.votes[0]
    bad_sig = bytes([v0.signature[0] ^ 0x01]) + v0.signature[1:]
    bad_cert = cert.__class__(
        header_digest=cert.header_digest,
        votes=(Vote(signer=v0.signer, signature=bad_sig), *cert.votes[1:]),
    )
    with pytest.raises(Exception) as ei:
        verify_certificate(header, bad_cert, b.genesis_secrets.committee)
    assert ei.value.code == ErrorCode.SIGNATURE_INVALID


def test_certificate_bound_to_different_header_rejected():
    b = ChainBuilder()
    b.genesis()
    header, cert = _signed_block(b, ["c0-a", "c0-b"])
    other = build_header(
        chain_id=b.chain_id, height=2, round=2, epoch=0,
        timestamp=1_000_020, parent_digest=codec.header_digest(header),
    )
    with pytest.raises(Exception) as ei:
        verify_certificate(other, cert, b.genesis_secrets.committee)
    assert ei.value.code == ErrorCode.SIGNATURE_INVALID
    assert "different header" in ei.value.reason


def test_signing_with_invalid_seed_is_compute_failure():
    from lightclient.crypto import sign_header
    from lightclient.errors import ComputeFailed

    b = ChainBuilder()
    b.genesis()
    header, _cert = _signed_block(b, ["c0-a"])
    with pytest.raises(ComputeFailed) as ei:
        sign_header(b"too-short-seed", header)
    assert ei.value.category.value == "compute"
