"""Duplicate re-transmission vs. exact double-vote conflict; bad signatures."""

from __future__ import annotations

from ffg_slash.models import IngestStatus, Offense, RejectReason, vote_from_envelope

from .conftest import CHAIN_ID, corrupt_signature, make_vote


def test_identical_retransmission_is_duplicate_not_offense(standard_service, keys):
    svc = standard_service
    seed, pub = keys["alpha"]
    v = make_vote(seed, pub, source_epoch=1, target_epoch=2)

    r1 = svc.ingest(v)
    assert r1.status is IngestStatus.ACCEPTED
    assert r1.evidences == []
    assert r1.seq == 1

    # exact same object/bytes again -> duplicate, no evidence, no seq
    r2 = svc.ingest(v)
    assert r2.status is IngestStatus.DUPLICATE
    assert r2.seq is None
    assert r2.evidences == []

    # identical content re-wrapped from envelope is still the same vote
    r3 = svc.ingest_raw(v.to_envelope())
    assert r3.status is IngestStatus.DUPLICATE

    stats = svc.stats()
    assert stats["received"] == 3
    assert stats["accepted"] == 1
    assert stats["duplicate"] == 2
    assert stats["real_conflicts_total"] == 0
    assert stats["evidences_created"] == 0
    assert len(svc.storage.list_evidences()) == 0


def test_same_target_different_root_is_double_vote(standard_service, keys):
    svc = standard_service
    seed, pub = keys["alpha"]
    v1 = make_vote(seed, pub, source_epoch=1, target_epoch=2,
                   target_root=b"\xAA" * 32)
    v2 = make_vote(seed, pub, source_epoch=1, target_epoch=2,
                   target_root=b"\xBB" * 32)

    r1 = svc.ingest(v1)
    assert r1.status is IngestStatus.ACCEPTED
    assert r1.evidences == []

    r2 = svc.ingest(v2)
    assert r2.status is IngestStatus.ACCEPTED  # conflicting vote is still logged
    assert len(r2.evidences) == 1
    ev = r2.evidences[0]
    assert ev.offense is Offense.DOUBLE_VOTE
    assert ev.packet["validator_pubkey"] == pub.hex()
    # slashable weight taken from the target epoch snapshot (alpha weight 1)
    assert ev.packet["slashable_weight"]["total_weight"] == 1

    stats = svc.stats()
    assert stats["real_conflicts_total"] == 1
    assert stats["offenses_by_validator"][pub.hex()] == {"double_vote": 1}
    # penalty mark exists and is keyed to the epoch
    marks = svc.storage.slash_marks()
    assert len(marks) == 1
    assert marks[0]["epoch"] == 2 and marks[0]["weight"] == 1


def test_third_conflict_vote_adds_no_duplicate_evidence(standard_service, keys):
    svc = standard_service
    seed, pub = keys["alpha"]
    svc.ingest(make_vote(seed, pub, source_epoch=1, target_epoch=2,
                         target_root=b"\x01" * 32))
    svc.ingest(make_vote(seed, pub, source_epoch=1, target_epoch=2,
                         target_root=b"\x02" * 32))
    # conflicts with both prior, but each pair is a distinct evidence;
    # replaying the identical second vote must create nothing new
    dup = make_vote(seed, pub, source_epoch=1, target_epoch=2,
                    target_root=b"\x02" * 32)
    assert svc.ingest(dup).status is IngestStatus.DUPLICATE
    assert svc.stats()["evidences_created"] == 1


def test_forged_signature_is_rejected_and_creates_no_evidence(standard_service, keys):
    """Unsigned/forged input MUST NOT be able to trigger a penalty mark."""
    svc = standard_service
    seed, pub = keys["alpha"]
    good = make_vote(seed, pub, source_epoch=1, target_epoch=2,
                     target_root=b"\xAA" * 32)
    forged = corrupt_signature(make_vote(
        seed, pub, source_epoch=1, target_epoch=2, target_root=b"\xBB" * 32))

    rg = svc.ingest(good)
    assert rg.status is IngestStatus.ACCEPTED
    rf = svc.ingest(forged)
    assert rf.status is IngestStatus.REJECTED
    assert rf.reject_reason is RejectReason.INVALID_SIGNATURE
    assert rf.evidences == []

    stats = svc.stats()
    assert stats["invalid_signatures"] == 1
    assert stats["rejected_by_reason"]["invalid_signature"] == 1
    # forged vote is not stored -> cannot produce evidence even retroactively
    assert stats["real_conflicts_total"] == 0
    assert svc.storage.list_evidences() == []
    assert svc.storage.slash_marks() == []


def test_wrong_chain_and_epoch_order_have_distinct_reasons(standard_service, keys):
    svc = standard_service
    seed, pub = keys["alpha"]
    bad_chain = make_vote(seed, pub, chain_id=CHAIN_ID + 9,
                          source_epoch=1, target_epoch=2)
    bad_order = make_vote(seed, pub, source_epoch=2, target_epoch=2)

    r1 = svc.ingest(bad_chain)
    assert r1.reject_reason is RejectReason.BAD_CHAIN_ID
    r2 = svc.ingest(bad_order)
    assert r2.reject_reason is RejectReason.BAD_EPOCH_ORDER

    reasons = svc.stats()["rejected_by_reason"]
    assert reasons["bad_chain_id"] == 1
    assert reasons["bad_epoch_order"] == 1


def test_malformed_envelope_is_rejected_not_accepted(standard_service):
    svc = standard_service
    r = svc.ingest_raw({"chain_id": CHAIN_ID})  # missing fields
    assert r.status is IngestStatus.REJECTED
    assert r.reject_reason is RejectReason.MALFORMED
    r2 = svc.ingest_raw("not-json-at-all")
    assert r2.status is IngestStatus.REJECTED
    assert r2.reject_reason is RejectReason.MALFORMED
    parsed = vote_from_envelope  # import-time check that parser exists
    assert svc.stats()["rejected_by_reason"]["malformed"] == 2
