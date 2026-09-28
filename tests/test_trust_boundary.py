"""Trust-period boundary tests, including the exact boundary equality.

The rule is ``accepted iff header.timestamp - tip.timestamp <= trust_period``
and the new timestamp is strictly greater. One second past the period is a
hard NEED_CHECKPOINT, not a warning.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lightclient import codec
from lightclient.errors import ErrorCode

GOLDEN = json.loads((Path(__file__).parent / "golden_vectors.json").read_text())
GENESIS_TS = GOLDEN["genesis_timestamp"]
PERIOD = GOLDEN["trust_period_seconds"]


def _block(builder, timestamp):
    return builder.add_block(
        signer_labels=["c0-a", "c0-b"], timestamp=timestamp
    )


def test_header_exactly_at_boundary_is_accepted(bootstrapped):
    kernel, builder = bootstrapped
    blk = _block(builder, GENESIS_TS + PERIOD)
    result = kernel.apply_header(blk.header, blk.certificate)
    assert result.decision == "accepted"
    assert result.tip.timestamp == GENESIS_TS + PERIOD


def test_header_one_second_beyond_needs_checkpoint(bootstrapped):
    kernel, builder = bootstrapped
    before = kernel.tip().digest
    blk = _block(builder, GENESIS_TS + PERIOD + 1)
    with pytest.raises(Exception) as ei:
        kernel.apply_header(blk.header, blk.certificate)
    assert ei.value.code == ErrorCode.NEED_CHECKPOINT
    assert ei.value.category.value == "state"
    assert ei.value.detail["gap_seconds"] == PERIOD + 1
    assert ei.value.detail["trust_period_seconds"] == PERIOD
    assert kernel.tip().digest == before
    # a valid in-period header must still work afterwards (cursor rewound)
    ok = builder.add_block(
        signer_labels=["c0-a", "c0-b"],
        timestamp=GENESIS_TS + 30,
        height=1,
        round=1,
        parent_digest=before,
    )
    assert kernel.apply_header(ok.header, ok.certificate).decision == "accepted"


def test_equal_timestamp_is_order_violation_not_checkpoint(bootstrapped):
    kernel, builder = bootstrapped
    blk = _block(builder, GENESIS_TS)  # gap 0
    with pytest.raises(Exception) as ei:
        kernel.apply_header(blk.header, blk.certificate)
    assert ei.value.code == ErrorCode.TIMESTAMP_NOT_MONOTONIC
    assert ei.value.code != ErrorCode.NEED_CHECKPOINT


def test_long_offline_chain_stops_at_first_lapsed_header(bootstrapped):
    """Simulate being offline for many headers' worth of time: the FIRST
    header whose timestamp leaves the period forces a fresh checkpoint;
    replay must stop there and no later header is attempted."""
    kernel, builder = bootstrapped
    from lightclient.replay import ReplayEngine, ReplayItem

    genesis_digest = kernel.tip().digest
    in_window = builder.add_block(
        signer_labels=["c0-a", "c0-b"], timestamp=GENESIS_TS + 600
    )
    # build the lapsed header connected to in_window (the expected new tip)
    lapsed = builder.add_block(
        signer_labels=["c0-a", "c0-b"],
        timestamp=GENESIS_TS + 600 + PERIOD + 1,
        height=2,
        round=2,
        parent_digest=codec.header_digest(
            in_window.header
        ),
    )
    # and one more validly-connected header that must NEVER be attempted
    later = builder.add_block(
        signer_labels=["c0-a", "c0-b"],
        timestamp=lapsed.header.timestamp + 10,
        height=3,
        round=3,
        parent_digest=codec.header_digest(
            lapsed.header
        ),
    )
    items = [
        ReplayItem(in_window.header, in_window.certificate),
        ReplayItem(lapsed.header, lapsed.certificate),
        ReplayItem(later.header, later.certificate),
    ]
    report = ReplayEngine(kernel).replay(items)
    assert report.ok is False
    assert report.status == "stopped"
    assert report.applied == 1
    assert report.failure_index == 1
    assert report.steps[1].error_code == ErrorCode.NEED_CHECKPOINT.value
    # the tip advanced exactly once and no further
    assert kernel.tip().height == 1
    assert kernel.tip().timestamp == GENESIS_TS + 600
    assert genesis_digest != kernel.tip().digest


def test_boundary_arithmetic_matches_independent_golden():
    tb = GOLDEN["trust_boundary"]
    # independently derived answers
    assert (tb["boundary_gap"] <= PERIOD) is True
    assert tb["boundary_accepted"] is True
    assert tb["beyond_needs_checkpoint"] is True
    assert tb["beyond_boundary_ts"] - tb["tip_timestamp"] == PERIOD + 1
