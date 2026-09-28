"""Explicit simplified finality rule (consecutive epoch supermajority links)."""

from __future__ import annotations

from ffg_slash.models import IngestStatus
from ffg_slash.state import reaches_quorum

from .conftest import GENESIS_ROOT, make_vote


def test_quorum_is_strict_two_thirds(standard_service):
    registry = standard_service.registry
    snap = registry.snapshot(2)
    total = snap.total_weight()
    assert total == 4
    assert reaches_quorum(2, snap) is False   # 2/4 = 1/2 is NOT enough
    assert reaches_quorum(3, snap) is True    # 3/4 >= 2/3


def _vote_epoch(svc, keys, labels, src, tgt, src_root=None, tgt_root=None):
    last = None
    for label in labels:
        seed, pub = keys[label]
        last = svc.ingest(make_vote(seed, pub, source_epoch=src, target_epoch=tgt,
                                    source_root=src_root, target_root=tgt_root))
    return last


def test_consecutive_links_justify_and_finalize(standard_service, keys):
    svc = standard_service
    # 3/4 vote genesis(0) -> 1: justifies epoch 1, finalizes genesis epoch 0
    r1 = _vote_epoch(svc, keys, ["alpha", "bravo", "charlie"],
                     0, 1, src_root=GENESIS_ROOT, tgt_root=b"\x01" * 32)
    assert r1.quorum is True
    assert r1.justified is not None and r1.justified.epoch == 1
    assert r1.finalized is not None and r1.finalized.epoch == 0

    # 3/4 then vote 1 -> 2 from the new justified checkpoint
    r2 = _vote_epoch(svc, keys, ["alpha", "bravo", "charlie"],
                     1, 2, src_root=b"\x01" * 32, tgt_root=b"\x02" * 32)
    assert r2.quorum is True
    assert r2.justified.epoch == 2
    assert r2.finalized.epoch == 1
    assert svc.state.snapshot_state()["finalized"]["epoch"] == 1


def test_non_consecutive_quorum_does_not_finalize(standard_service, keys):
    svc = standard_service
    # 3/4 vote 0 -> 3 while justified is genesis: dangling link, no finality
    r = _vote_epoch(svc, keys, ["alpha", "bravo", "charlie"],
                    0, 3, src_root=GENESIS_ROOT, tgt_root=b"\x03" * 32)
    assert r.quorum is True
    assert r.justified is None and r.finalized is None
    assert svc.state.snapshot_state()["justified"]["epoch"] == 0


def test_double_counting_same_voter_does_not_reach_quorum(standard_service, keys):
    svc = standard_service
    seed, pub = keys["alpha"]
    v = make_vote(seed, pub, source_epoch=0, target_epoch=1,
                  source_root=GENESIS_ROOT)
    svc.ingest(v)
    svc.ingest(v)
    third = svc.ingest(v)
    assert third.status is IngestStatus.DUPLICATE
    assert third.link_weight is None
    # only one distinct voter weight on the link
    assert svc.state.links.weight_on(
        (0, GENESIS_ROOT, 1, v.target_root)) == 1


def test_disagreement_on_target_root_splits_quorum(standard_service, keys):
    svc = standard_service
    # 2 vote root A, 2 vote root B at epoch 1 -> neither link justifies
    last = None
    for label in ["alpha", "bravo", "charlie", "delta"]:
        seed, pub = keys[label]
        root = b"\xAA" * 32 if label in ("alpha", "bravo") else b"\xBB" * 32
        last = svc.ingest(make_vote(seed, pub, source_epoch=0, target_epoch=1,
                                    source_root=GENESIS_ROOT, target_root=root))
    assert last.quorum is False
    assert svc.state.snapshot_state()["justified"]["epoch"] == 0
