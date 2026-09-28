"""Membership changes: weights come from target-epoch snapshots, old evidence stands."""

from __future__ import annotations

from ffg_slash.models import IngestStatus, Offense, RejectReason

from .conftest import make_vote


def test_validator_leaving_set_is_inactive_in_new_epoch(standard_service, keys):
    svc = standard_service
    seed_d, pub_d = keys["delta"]
    # epoch 2: delta is a member and can vote
    r_ok = svc.ingest(make_vote(seed_d, pub_d, source_epoch=1, target_epoch=2))
    assert r_ok.status is IngestStatus.ACCEPTED

    # epoch 3: delta rotated out -> inactive
    r_out = svc.ingest(make_vote(seed_d, pub_d, source_epoch=2, target_epoch=3))
    assert r_out.status is IngestStatus.REJECTED
    assert r_out.reject_reason is RejectReason.VALIDATOR_INACTIVE


def test_new_validator_only_active_after_joining(standard_service, keys):
    svc = standard_service
    seed_e, pub_e = keys["echo"]
    r_early = svc.ingest(make_vote(seed_e, pub_e, source_epoch=1, target_epoch=2))
    assert r_early.status is IngestStatus.REJECTED
    assert r_early.reject_reason in (
        RejectReason.UNKNOWN_VALIDATOR, RejectReason.VALIDATOR_INACTIVE)
    r_later = svc.ingest(make_vote(seed_e, pub_e, source_epoch=2, target_epoch=3))
    assert r_later.status is IngestStatus.ACCEPTED


def test_surround_spanning_membership_change_uses_each_epoch_snapshot(
        standard_service, keys):
    """outer vote 1->4, inner vote 2->3; membership rotates after epoch 2.

    Delta CANNOT vote in epoch 3+, so it cannot participate in a spanning
    surround — its inner vote is rejected. Alpha is present in both sets, so
    the evidence is valid and weight 1 is drawn from snapshots 4 and 3
    independently; the embedded epoch-3 snapshot proves the rotation.
    """
    svc = standard_service
    seed_d, pub_d = keys["delta"]
    # delta outer vote at target 2 (member), but inner at target 3 impossible
    svc.ingest(make_vote(seed_d, pub_d, source_epoch=1, target_epoch=2))
    r_blocked = svc.ingest(make_vote(seed_d, pub_d, source_epoch=2, target_epoch=3))
    assert r_blocked.reject_reason is RejectReason.VALIDATOR_INACTIVE

    seed_a, pub_a = keys["alpha"]
    outer = make_vote(seed_a, pub_a, source_epoch=1, target_epoch=4)
    inner = make_vote(seed_a, pub_a, source_epoch=2, target_epoch=3)
    assert svc.ingest(outer).status is IngestStatus.ACCEPTED
    r = svc.ingest(inner)
    assert r.status is IngestStatus.ACCEPTED
    assert len(r.evidences) == 1
    ev = r.evidences[0]
    assert ev.offense is Offense.SURROUND_VOTE
    epochs = {item["epoch"]: item["weight"]
              for item in ev.packet["slashable_weight"]["epochs"]}
    assert epochs == {4: 1, 3: 1}
    # snapshots embedded in evidence prove membership at each target epoch
    pks_3 = {m["pubkey"] for m in ev.packet["vote_2_snapshot"]["members"]}
    assert keys["delta"][1].hex() not in pks_3
    assert keys["echo"][1].hex() in pks_3


def test_changed_weights_reflect_in_slashable_weight(keys, service_factory):
    seed_a, pub_a = keys["alpha"]
    svc = service_factory(
        {2: ["alpha", "bravo"], 3: ["alpha", "bravo"]},
        weights={"alpha": 1, "bravo": 4})
    v1 = make_vote(seed_a, pub_a, source_epoch=1, target_epoch=2,
                   target_root=b"\xAA" * 32)
    v2 = make_vote(seed_a, pub_a, source_epoch=1, target_epoch=2,
                   target_root=b"\xBB" * 32)
    svc.ingest(v1)
    r = svc.ingest(v2)
    # double vote in ONE target epoch -> slashable weight = alpha weight at 2
    assert r.evidences[0].packet["slashable_weight"] == {
        "epochs": [{"epoch": 2, "weight": 1}], "total_weight": 1}
