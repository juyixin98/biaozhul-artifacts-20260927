"""Rejection: conflicting/equivocating headers and untrusted branches."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lightclient import codec
from lightclient.errors import ErrorCode
from lightclient.fixtures.builder import build_certificate, build_header

GOLDEN = json.loads((Path(__file__).parent / "golden_vectors.json").read_text())


def _snapshot(kernel):
    return (
        kernel.tip().digest,
        kernel.tip().height,
        kernel.store.all_header_digests(),
    )


def test_golden_equivocation_same_height_conflict(bootstrapped):
    kernel, builder = bootstrapped
    # accept golden h1 first
    h1 = GOLDEN["headers"][0]
    result = kernel.apply_header(
        codec.decode_header(bytes.fromhex(h1["wire_hex"])),
        codec.decode_certificate(bytes.fromhex(h1["cert_hex"])),
    )
    assert result.decision == "accepted"
    before = _snapshot(kernel)

    # conflict vector: same height 1 / same parent, different payload, valid
    # threshold signatures from the real committee
    conflict = GOLDEN["conflict"]
    with pytest.raises(Exception) as ei:
        kernel.apply_header_wire(
            bytes.fromhex(conflict["wire_hex"]),
            bytes.fromhex(conflict["cert_hex"]),
        )
    assert ei.value.code == ErrorCode.CONFLICTING_HEADER
    assert ei.value.category.value == "state"
    assert conflict["differs_from_h1"] is True
    assert _snapshot(kernel) == before


def test_known_but_non_tip_parent_is_untrusted_branch(bootstrapped):
    kernel, builder = bootstrapped
    h1 = builder.add_block(signer_labels=["c0-a", "c0-b"])
    kernel.apply_header(h1.header, h1.certificate)
    h2 = builder.add_block(signer_labels=["c0-b", "c0-c"])
    kernel.apply_header(h2.header, h2.certificate)

    before = _snapshot(kernel)
    # A header at the next height that claims h1 (known, height 1) as its
    # parent instead of the tip h2: it skips the trusted tip and tries to
    # continue a different branch — the parent is known but not trusted.
    fork_header = build_header(
        chain_id=builder.chain_id,
        height=3,  # extends tip's *height* but not the tip itself
        round=9,
        epoch=0,
        timestamp=h2.header.timestamp + 5,
        parent_digest=codec.header_digest(h1.header),  # known, non-tip
        payload=b"\xaa" * 32,
    )
    fork_cert = build_certificate(
        fork_header,
        [
            builder.genesis_secrets.seed_for("c0-a"),
            builder.genesis_secrets.seed_for("c0-b"),
        ],
    )
    with pytest.raises(Exception) as ei:
        kernel.apply_header(fork_header, fork_cert)
    assert ei.value.code == ErrorCode.CONFLICTING_HEADER
    assert "untrusted branch" in ei.value.reason
    assert _snapshot(kernel) == before

    # chain still extends from the canonical tip afterwards
    cont = builder.add_block(signer_labels=["c0-a", "c0-c"])
    result = kernel.apply_header(cont.header, cont.certificate)
    assert result.decision == "accepted"


def test_unknown_parent_rejected(bootstrapped):
    kernel, builder = bootstrapped
    before = _snapshot(kernel)
    blk = builder.add_block(
        signer_labels=["c0-a", "c0-b"], parent_digest=b"\x77" * 32
    )
    with pytest.raises(Exception) as ei:
        kernel.apply_header(blk.header, blk.certificate)
    assert ei.value.code == ErrorCode.PARENT_UNKNOWN
    assert _snapshot(kernel) == before


def test_height_gap_is_untrusted_branch(bootstrapped):
    kernel, builder = bootstrapped
    h1 = builder.add_block(signer_labels=["c0-a", "c0-b"])
    kernel.apply_header(h1.header, h1.certificate)
    before = _snapshot(kernel)
    # height 3 directly, with a fabricated parent not equal to tip
    blk = builder.add_block(
        signer_labels=["c0-a", "c0-b"], height=3
    )
    with pytest.raises(Exception) as ei:
        kernel.apply_header(blk.header, blk.certificate)
    assert ei.value.code == ErrorCode.UNTRUSTED_BRANCH
    assert "height gap" in ei.value.reason
    assert _snapshot(kernel) == before


def test_round_must_strictly_increase(bootstrapped):
    kernel, builder = bootstrapped
    h1 = builder.add_block(signer_labels=["c0-a", "c0-b"])  # canonical, round 1
    kernel.apply_header(h1.header, h1.certificate)
    before = _snapshot(kernel)
    # equal round -> rejected
    blk = builder.add_block(signer_labels=["c0-a", "c0-b"], round=1)
    with pytest.raises(Exception) as ei:
        kernel.apply_header(blk.header, blk.certificate)
    assert ei.value.code == ErrorCode.ROUND_NOT_MONOTONIC
    assert ei.value.detail["tip_round"] == 1
    assert ei.value.detail["got_round"] == 1
    assert _snapshot(kernel) == before
    # strict increase then succeeds (rejected override did not move cursor)
    good = builder.add_block(signer_labels=["c0-a", "c0-b"], round=7)
    assert kernel.apply_header(good.header, good.certificate).decision == "accepted"


def test_chain_id_mismatch_is_input_error_and_does_not_root(bootstrapped):
    kernel, builder = bootstrapped
    before = _snapshot(kernel)
    blk = builder.add_block(signer_labels=["c0-a", "c0-b"])
    # rewire header to a foreign chain via a fresh build
    foreign = build_header(
        chain_id="some-other-chain",
        height=blk.header.height,
        round=blk.header.round,
        epoch=blk.header.epoch,
        timestamp=blk.header.timestamp,
        parent_digest=blk.header.parent_digest,
    )
    cert = build_certificate(
        foreign,
        [
            builder.genesis_secrets.seed_for("c0-a"),
            builder.genesis_secrets.seed_for("c0-b"),
        ],
    )
    with pytest.raises(Exception) as ei:
        kernel.apply_header(foreign, cert)
    assert ei.value.code == ErrorCode.CHAIN_MISMATCH
    assert ei.value.category.value == "input"
    assert _snapshot(kernel) == before
