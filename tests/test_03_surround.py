"""Surround-vote detection: strict nesting, ordering independence, boundaries."""

from __future__ import annotations

from ffg_slash.evidence import classify_conflict, surround_pair
from ffg_slash.models import IngestStatus, Offense

from .conftest import make_vote


def _v(seed, pub, s, t):
    return make_vote(seed, pub, source_epoch=s, target_epoch=t)


def test_strict_nesting_in_both_orders(keys):
    seed, pub = keys["bravo"]
    outer = _v(seed, pub, 1, 6)
    inner = _v(seed, pub, 2, 5)
    pair = surround_pair(outer, inner)
    assert pair == (outer, inner)
    # reverse submission order is detected identically
    assert surround_pair(inner, outer) == (outer, inner)
    assert classify_conflict(outer, inner) is Offense.SURROUND_VOTE


def test_equal_boundaries_are_not_surround(keys):
    seed, pub = keys["bravo"]
    # equal inner target == outer target
    assert surround_pair(_v(seed, pub, 1, 5), _v(seed, pub, 2, 5)) is None
    # equal source
    assert surround_pair(_v(seed, pub, 2, 6), _v(seed, pub, 2, 5)) is None
    # adjacent non-nested (same source/target structure, no containment)
    assert classify_conflict(_v(seed, pub, 1, 3), _v(seed, pub, 3, 5)) is None


def test_surround_evidence_through_service(keys, service_factory):
    svc = service_factory({
        e: ["alpha", "bravo", "charlie", "delta"] for e in range(1, 7)
    })
    seed, pub = keys["alpha"]
    outer = _v(seed, pub, 1, 6)
    inner = _v(seed, pub, 2, 5)

    r1 = svc.ingest(outer)
    assert r1.evidences == []
    r2 = svc.ingest(inner)
    assert r2.status is IngestStatus.ACCEPTED
    assert len(r2.evidences) == 1
    assert r2.evidences[0].offense is Offense.SURROUND_VOTE
    # weights come from BOTH target epochs (inner t=5, outer t=6), weight 1 each
    assert r2.evidences[0].packet["slashable_weight"]["total_weight"] == 2


def test_surround_reverse_submission(keys, service_factory):
    svc = service_factory({
        2: ["alpha", "bravo", "charlie", "delta"],
        3: ["alpha", "bravo", "charlie", "delta"],
        4: ["alpha", "bravo", "charlie", "delta"],
        5: ["alpha", "bravo", "charlie", "delta"],
        6: ["alpha", "bravo", "charlie", "delta"],
        7: ["alpha", "bravo", "charlie", "delta"],
    })
    seed, pub = keys["alpha"]
    inner = _v(seed, pub, 2, 5)
    outer = _v(seed, pub, 1, 6)
    assert svc.ingest(inner).evidences == []
    r = svc.ingest(outer)
    assert len(r.evidences) == 1
    assert r.evidences[0].offense is Offense.SURROUND_VOTE
    stats = svc.stats()
    assert stats["real_conflicts_total"] == 1
    assert stats["offenses_by_validator"][pub.hex()] == {"surround_vote": 1}
