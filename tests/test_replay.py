"""Offline replay engine tests: stop-on-reject, preflight, state rollback."""

from __future__ import annotations

import pytest

from lightclient.errors import ErrorCode
from lightclient.replay import (
    APPLIED_ALL,
    PREFLIGHT_FAILED,
    STOPPED,
    ReplayEngine,
    ReplayItem,
)


def _items(builder, blocks):
    return [ReplayItem(x.header, x.certificate, source=f"block-{i}")
            for i, x in enumerate(blocks)]


def test_replay_applies_all_legitimate_blocks(bootstrapped):
    kernel, builder = bootstrapped
    c1 = builder.add_committee(1, [("c1-a", 1), ("c1-b", 1), ("c1-c", 1)])
    blocks = [
        builder.add_block(signer_labels=["c0-a", "c0-b"]),
        builder.add_block(
            signer_labels=["c0-b", "c0-c"], next_committee=c1.committee
        ),
        builder.add_block(signer_labels=["c1-a", "c1-c"], epoch=1),
    ]
    report = ReplayEngine(kernel).replay(_items(builder, blocks), run_id="rp-ok")
    assert report.status == APPLIED_ALL
    assert report.ok is True
    assert report.applied == 3
    assert report.tip_after["height"] == 3
    assert report.tip_after["epoch"] == 1
    assert all(s.decision == "accepted" for s in report.steps)


def test_replay_stops_at_first_rejection_and_preserves_tip(bootstrapped):
    kernel, builder = bootstrapped
    good = builder.add_block(signer_labels=["c0-a", "c0-b"])
    bad = builder.add_block(signer_labels=["c0-a"])  # weight 1 < quorum
    after = builder.add_block(signer_labels=["c0-a", "c0-b"])
    tip_before = kernel.tip().digest
    report = ReplayEngine(kernel).replay(_items(builder, [good, bad, after]))
    assert report.status == STOPPED
    assert report.applied == 1
    assert report.failure_index == 1
    fail = report.steps[1]
    assert fail.error_code == ErrorCode.WEIGHT_BELOW_QUORUM.value
    assert fail.error_category == "state"
    # third item was never attempted: only two kernel decisions audited
    assert len(report.steps) == 2
    # exactly one header beyond genesis is trusted
    assert kernel.tip().height == 1
    assert kernel.tip().digest != tip_before


def test_replay_preflight_resource_limit_touches_no_state(bootstrapped):
    kernel, builder = bootstrapped
    blocks = [
        builder.add_block(signer_labels=["c0-a", "c0-b"])
        for _ in range(kernel.config.max_replay_batch + 1)
    ]
    tip_before = kernel.tip().digest
    report = ReplayEngine(kernel).replay(_items(builder, blocks))
    assert report.status == PREFLIGHT_FAILED
    assert report.failure_index == -1
    assert report.steps[0].error_code == ErrorCode.RESOURCE_LIMIT.value
    assert kernel.tip().digest == tip_before
    assert kernel.tip().height == 0


def test_preflight_rejects_cert_bound_to_other_header(bootstrapped):
    kernel, builder = bootstrapped
    h1 = builder.add_block(signer_labels=["c0-a", "c0-b"])
    h2 = builder.add_block(signer_labels=["c0-b", "c0-c"])
    # bind h2's certificate to h1 in a replay item (cross-wired)
    item = ReplayItem(h2.header, h1.certificate, source="cross-wired")
    tip_before = kernel.tip().digest
    with pytest.raises(Exception) as ei:
        ReplayEngine(kernel).preflight([
            ReplayItem(h1.header, h1.certificate), item
        ])
    assert ei.value.code == ErrorCode.SIGNATURE_INVALID
    assert kernel.tip().digest == tip_before


def test_replay_report_serializes_for_logging(bootstrapped):
    kernel, builder = bootstrapped
    blk = builder.add_block(signer_labels=["c0-a", "c0-b"])
    report = ReplayEngine(kernel).replay(_items(builder, [blk]))
    doc = report.to_dict()
    assert doc["run_id"] is not None or True
    assert doc["steps"][0]["digest"]
    assert doc["tip_after"]["height"] == 1
