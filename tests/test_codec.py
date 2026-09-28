"""Encoding boundary tests: round-trip determinism and strict decoding."""

from __future__ import annotations

import pytest

from lightclient import codec
from lightclient.errors import ErrorCode
from lightclient.fixtures.builder import (
    ChainBuilder,
    build_header,
)
from lightclient.types import GENESIS_PARENT


def test_header_codec_roundtrip_and_deterministic_digest():
    b = ChainBuilder()
    c1 = b.add_committee(1, [("x", 1), ("y", 2)])
    h = build_header(
        chain_id=b.chain_id,
        height=3,
        round=9,
        epoch=0,
        timestamp=42,
        parent_digest=GENESIS_PARENT,
        next_committee=c1.committee,
    )
    wire = codec.encode_header(h)
    again = codec.encode_header(h)
    assert wire == again  # canonical, deterministic
    decoded = codec.decode_header(wire)
    assert decoded == h
    assert decoded.next_committee == c1.committee
    # digest stability
    assert codec.header_digest(decoded).hex() == codec.header_digest(h).hex()


def test_tag_domain_separation_rejects_cross_type_bytes():
    b = ChainBuilder()
    committee_wire = codec.encode_committee(b.genesis_secrets.committee)
    # A committee byte string must not decode as a header/certificate.
    with pytest.raises(Exception) as ei:
        codec.decode_header(committee_wire)
    assert ei.value.code == ErrorCode.INPUT_MALFORMED
    with pytest.raises(Exception) as ei2:
        codec.decode_certificate(committee_wire)
    assert ei2.value.code == ErrorCode.INPUT_MALFORMED


@pytest.mark.parametrize(
    "mutator",
    [
        lambda w: b"",  # empty
        lambda w: w[:-1],  # truncated
        lambda w: w + b"\x00",  # trailing byte
        lambda w: b"XXXX" + w[4:],  # bad tag
        lambda w: bytes([w[0] ^ 0xFF]) + w[1:],  # flipped first byte
    ],
)
def test_header_decoder_rejects_malformed(mutator):
    b = ChainBuilder()
    h = build_header(
        chain_id=b.chain_id, height=0, round=0, epoch=0,
        timestamp=1, parent_digest=GENESIS_PARENT
    )
    wire = mutator(codec.encode_header(h))
    with pytest.raises(Exception) as ei:
        codec.decode_header(wire)
    assert ei.value.code == ErrorCode.INPUT_MALFORMED


def test_length_limits_enforced_at_decode():
    b = ChainBuilder()
    # Committee embedded in a header cannot exceed the supplied member bound.
    c_big = b.add_committee(1, [(f"m{i}", 1) for i in range(10)])
    h2 = build_header(
        chain_id=b.chain_id, height=2, round=2, epoch=0, timestamp=2,
        parent_digest=GENESIS_PARENT, next_committee=c_big.committee,
    )
    with pytest.raises(Exception) as ei:
        codec.decode_header(
            codec.encode_header(h2), max_committee_members=4
        )
    # A collection bound is a resource exhaustion, not a shape error.
    assert ei.value.code == ErrorCode.RESOURCE_LIMIT
    assert ei.value.category.value == "resource"
    assert ei.value.detail["count"] == 10
    assert ei.value.detail["limit"] == 4


def test_certificate_codec_binding_digest():
    b = ChainBuilder()
    _g, env = b.genesis()
    blk = b.add_block(signer_labels=["c0-a", "c0-b"])
    wire = codec.encode_certificate(blk.certificate)
    cert = codec.decode_certificate(wire)
    assert cert == blk.certificate
    assert cert.header_digest == codec.header_digest(blk.header)
