"""Rejection: insufficient threshold weight — including the exact boundary."""

from __future__ import annotations

import pytest

from lightclient import codec
from lightclient.errors import ErrorCode
from lightclient.fixtures.builder import ChainBuilder, build_header


def _snapshot(kernel):
    return (
        kernel.tip().digest,
        kernel.tip().height,
        kernel.tip().round,
        kernel.tip().timestamp,
        kernel.store.all_header_digests(),
        kernel.store.all_committee_ids(),
    )


def test_single_weight_one_below_quorum_two_rejected(bootstrapped):
    kernel, builder = bootstrapped
    before = _snapshot(kernel)
    blk = builder.add_block(signer_labels=["c0-a"])  # weight 1 < quorum 2
    with pytest.raises(Exception) as ei:
        kernel.apply_header(blk.header, blk.certificate)
    assert ei.value.code == ErrorCode.WEIGHT_BELOW_QUORUM
    assert ei.value.category.value == "state"
    assert ei.value.detail == {
        "signed_weight": 1,
        "quorum_weight": 2,
        "participant_count": 1,
    }
    assert _snapshot(kernel) == before  # trusted state untouched


def test_weight_boundary_equality_is_accepted_then_one_more_rejected(bootstrapped):
    kernel, builder = bootstrapped
    # exactly quorum -> accepted
    ok = builder.add_block(signer_labels=["c0-a", "c0-b"])
    result = kernel.apply_header(ok.header, ok.certificate)
    assert result.certificate.signed_weight == 2
    assert result.decision == "accepted"

    before = _snapshot(kernel)
    low = builder.add_block(signer_labels=["c0-a"])  # weight 1
    with pytest.raises(Exception) as ei:
        kernel.apply_header(low.header, low.certificate)
    assert ei.value.code == ErrorCode.WEIGHT_BELOW_QUORUM
    assert ei.value.detail["signed_weight"] == 1
    assert _snapshot(kernel) == before


def test_weighted_committee_single_heavy_member_reaches_quorum(tmp_path):
    # weights [2,2,1], quorum 2: one weight-2 member suffices (weight, not
    # headcount); two weight-1 members do not exist here, but a single w1 in
    # the default committee must still fail.
    builder = ChainBuilder(genesis_members=[("h2", 2), ("h2b", 2), ("l1", 1)])
    from lightclient.config import LightClientConfig
    from lightclient.kernel import LightClientKernel
    from lightclient.store import Store

    _g, env = builder.genesis()
    kernel = LightClientKernel(
        Store(":memory:"), LightClientConfig(), builder.checkpoint_pub
    )
    kernel.bootstrap(env)

    blk = builder.add_block(signer_labels=["h2"])
    result = kernel.apply_header(blk.header, blk.certificate)
    assert result.decision == "accepted"
    assert result.certificate.participant_count == 1
    assert result.certificate.signed_weight == 2


def test_empty_and_oversized_certificates_classified_as_input_or_resource(
    bootstrapped,
):
    kernel, builder = bootstrapped
    from lightclient.types import Certificate, Vote

    header = build_header(
        chain_id=builder.chain_id,
        height=1,
        round=1,
        epoch=0,
        timestamp=1_000_010,
        parent_digest=builder.tip_digest,
    )
    # zero votes cannot even be constructed (type invariant); wire malformed
    # cert -> INPUT_MALFORMED
    bad_cert_wire = b"CERT1" + b"\x00" * 8
    with pytest.raises(Exception) as ei:
        kernel.apply_header_wire(
            codec.encode_header(header), bad_cert_wire
        )
    assert ei.value.code == ErrorCode.INPUT_MALFORMED

    # too many votes bound -> RESOURCE_LIMIT at decode
    votes = tuple(
        Vote(signer=bytes([i + 1]) * 32, signature=b"\x00" * 64)
        for i in range(kernel.config.max_certificate_votes + 1)
    )
    cert = Certificate(header_digest=codec.header_digest(header), votes=votes)
    wire = codec.encode_certificate(cert)
    with pytest.raises(Exception) as ri:
        kernel.apply_header_wire(codec.encode_header(header), wire)
    assert ri.value.code == ErrorCode.RESOURCE_LIMIT
    assert ri.value.category.value == "resource"
