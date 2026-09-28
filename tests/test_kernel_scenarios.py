"""Kernel scenario tests — exact classifications vs the independent oracle.

Required scenarios are all asserted with concrete outcomes and failure
categories: duplicate retransmit, same-target double vote, nested/surround
rounds (both directions), boundary equality, crossing links, membership
change, weight snapshots, bad signatures and unknown validators.
"""
from __future__ import annotations

import pytest

from localffg.kernel import SlashingKernel, is_double_vote, is_surround, votes_identical
from localffg.models import SignedVote, ViolationKind, VoteStatus

from independent_oracle import expected_sequence

# scenario name -> exact per-step statuses expected (mirrors oracle)
EXPECTED_STATUSES = {
    "S1_duplicate_retransmit": ["accepted", "duplicate_retransmit", "duplicate_retransmit"],
    "S2_double_vote_same_target": ["accepted", "double_vote"],
    "S3_surround_nested": ["accepted", "surround_vote"],
    "S3b_surround_reverse_order": ["accepted", "surround_vote"],
    "S3c_boundary_equality_no_offense": ["accepted", "accepted"],
    "S3d_crossing_no_surround": ["accepted", "accepted"],
    "S4_membership_change": [
        "invalid_membership",  # dave votes before joining
        "accepted",           # dave after join
        "accepted",           # erin while active
        "invalid_membership", # erin after exit
    ],
    "S5_bad_signature": ["invalid_signature"],
    "S5b_wrong_signer_pubkey": ["invalid_signature"],
    "S6_unknown_validator": ["unknown_validator"],
    "S7_wrong_chain": ["invalid_chain"],
    "S8_bad_rounds": ["invalid_rounds"],
    "S9_cross_validator_no_conflict": ["accepted", "accepted"],
    "S10_weight_snapshot_offense": ["accepted", "double_vote"],
    "S11_mixed_conflict_then_rexmit": ["accepted", "double_vote", "duplicate_retransmit"],
}


@pytest.mark.parametrize("scenario", sorted(EXPECTED_STATUSES))
def test_scenarios_match_exact_statuses_and_independent_oracle(
    scenario, manifest, domain, registry, oracle_registry
):
    steps = manifest["scenarios"][scenario]
    kernel = SlashingKernel(domain=domain, registry=registry)

    oracle_steps = expected_sequence(domain, oracle_registry, steps)
    actual: list[str] = []
    for step in steps:
        signed = SignedVote.from_json_dict(step["signed"])
        result = kernel.ingest(signed)
        actual.append(result.status.value)

    # 1) concrete hard-coded expectations
    assert actual == EXPECTED_STATUSES[scenario], f"{scenario}: {actual}"

    # 2) independent oracle agreement, step by step
    oracle_statuses = [o.status for o in oracle_steps]
    assert oracle_statuses == EXPECTED_STATUSES[scenario], f"oracle drift in {scenario}"
    assert actual == oracle_statuses, f"kernel vs oracle mismatch in {scenario}"


def test_duplicate_retransmit_is_not_an_offense(manifest, domain, registry):
    kernel = SlashingKernel(domain=domain, registry=registry)
    steps = manifest["scenarios"]["S1_duplicate_retransmit"]
    for step in steps:
        r = kernel.ingest(SignedVote.from_json_dict(step["signed"]))
        assert not r.slashable and r.evidence == []
    assert kernel.stats.accepted == 1
    assert kernel.stats.duplicate_retransmit == 2
    assert kernel.evidence() == []
    # validator has exactly one stored vote
    assert len(kernel.votes_of("alice")) == 1


def test_double_vote_emits_self_contained_evidence(manifest, domain, registry):
    kernel = SlashingKernel(domain=domain, registry=registry)
    steps = manifest["scenarios"]["S2_double_vote_same_target"]
    kernel.ingest(SignedVote.from_json_dict(steps[0]["signed"]))
    r = kernel.ingest(SignedVote.from_json_dict(steps[1]["signed"]))
    assert r.status is VoteStatus.DOUBLE_VOTE and len(r.evidence) == 1
    ev = r.evidence[0]
    assert ev.kind is ViolationKind.DOUBLE_VOTE
    assert ev.validator_id == "bob"
    # both votes present, independently verifiable content
    assert ev.vote_a.vote.target_round == ev.vote_b.vote.target_round == 8
    assert ev.vote_a.signature and ev.vote_b.signature
    assert ev.weight == 100 and ev.weight_epoch == 0
    # evidence is retrievable from kernel store
    assert kernel.has_evidence(ev.evidence_id)


def test_surround_detection_both_directions_and_predicate_precision(manifest, domain, registry):
    # direct predicate checks for exact definition
    def mk(vid, s, t, root):
        from localffg.models import Vote
        return SignedVote(vote=Vote("c", vid, s, t, root), signer_pubkey=b"k" * 32, signature=b"s" * 64)

    outer, inner = mk("v", 0, 15, b"a"), mk("v", 5, 10, b"b")
    sur, i, o = is_surround(outer, inner)
    assert sur and i is inner and o is outer
    sur, i, o = is_surround(inner, outer)
    assert sur and i is inner and o is outer

    # equal boundary pairs are NOT surround
    assert is_surround(mk("v", 0, 10, b"a"), mk("v", 10, 15, b"b"))[0] is False
    # same start [0,10] vs [0,15]: s2<s1 fails and reverse 0<0 fails -> not surround
    assert is_surround(mk("v", 0, 10, b"a"), mk("v", 0, 15, b"b"))[0] is False
    # crossing links [0,10] / [5,20] -> neither direction strict
    assert is_surround(mk("v", 0, 10, b"a"), mk("v", 5, 20, b"b"))[0] is False
    # different validators never conflict
    assert is_surround(mk("v1", 0, 15, b"a"), mk("v2", 5, 10, b"b"))[0] is False
    assert not is_double_vote(mk("v1", 5, 8, b"a"), mk("v2", 1, 8, b"b"))


def test_membership_change_exact_categories(manifest, domain, registry):
    kernel = SlashingKernel(domain=domain, registry=registry)
    statuses = [
        kernel.ingest(SignedVote.from_json_dict(s["signed"])).status
        for s in manifest["scenarios"]["S4_membership_change"]
    ]
    assert statuses == [
        VoteStatus.INVALID_MEMBERSHIP,
        VoteStatus.ACCEPTED,
        VoteStatus.ACCEPTED,
        VoteStatus.INVALID_MEMBERSHIP,
    ]
    assert kernel.stats.invalid_membership == 2
    # invalid membership votes must never create evidence ("unsigned" guard)
    assert kernel.evidence() == []


def test_invalid_signatures_counted_separately_from_conflicts(manifest, domain, registry):
    kernel = SlashingKernel(domain=domain, registry=registry)
    r1 = kernel.ingest(SignedVote.from_json_dict(manifest["scenarios"]["S5_bad_signature"][0]["signed"]))
    r2 = kernel.ingest(SignedVote.from_json_dict(manifest["scenarios"]["S5b_wrong_signer_pubkey"][0]["signed"]))
    assert r1.status is VoteStatus.INVALID_SIGNATURE and r1.reason.startswith("signature")
    assert r2.status is VoteStatus.INVALID_SIGNATURE and "pubkey_mismatch" in r2.reason
    assert kernel.stats.invalid_signature == 2
    assert kernel.stats.double_vote == 0 and kernel.stats.surround_vote == 0
    assert kernel.evidence() == []


def test_weight_taken_from_correct_epoch_snapshot(manifest, domain, registry):
    # alice: weight 100 @ep0, 150 @ep2. Offense at epoch 0 -> weight 100.
    kernel = SlashingKernel(domain=domain, registry=registry)
    steps = manifest["scenarios"]["S10_weight_snapshot_offense"]
    kernel.ingest(SignedVote.from_json_dict(steps[0]["signed"]))
    r = kernel.ingest(SignedVote.from_json_dict(steps[1]["signed"]))
    assert r.status is VoteStatus.DOUBLE_VOTE
    ev = r.evidence[0]
    assert ev.weight_epoch == 0 and ev.weight == 100


def test_surround_after_weight_change_uses_earlier_target_epoch(domain):
    """Build votes spanning epochs: outer targets epoch 2, inner targets
    epoch 0? Impossible for strict nesting order — instead construct
    outer [0,25] (target epoch 2) and inner [5,9] (target epoch 0). The
    earlier target is epoch 0 -> weight 100 for alice."""
    from localffg.crypto import Signer
    from localffg.epochs import ValidatorRegistry

    signer = Signer.from_seed("alice", b"localffg-fixture/alice")
    reg = ValidatorRegistry(chain_id="local-chain-0", epoch_length=10)
    reg.add_validator("alice", signer.public_key_bytes, 100, 0)
    reg.set_weight("alice", 2, 150)
    kernel = SlashingKernel(domain=domain, registry=reg)
    outer = signer.sign_vote(domain=domain, chain_id=reg.chain_id, source_round=0, target_round=25, block_root=b"o" + b"\x00" * 31)
    inner = signer.sign_vote(domain=domain, chain_id=reg.chain_id, source_round=5, target_round=9, block_root=b"i" + b"\x00" * 31)
    r1 = kernel.ingest(outer)
    r2 = kernel.ingest(inner)
    assert r1.status is VoteStatus.ACCEPTED
    assert r2.status is VoteStatus.SURROUND_VOTE
    ev = r2.evidence[0]
    assert min(ev.vote_a.vote.target_round, ev.vote_b.vote.target_round) == 9
    assert ev.weight_epoch == 0 and ev.weight == 100


def test_conflict_vote_still_recorded_and_rexmit_after_conflict(manifest, domain, registry):
    kernel = SlashingKernel(domain=domain, registry=registry)
    steps = manifest["scenarios"]["S11_mixed_conflict_then_rexmit"]
    r1 = kernel.ingest(SignedVote.from_json_dict(steps[0]["signed"]))
    r2 = kernel.ingest(SignedVote.from_json_dict(steps[1]["signed"]))
    r3 = kernel.ingest(SignedVote.from_json_dict(steps[2]["signed"]))
    assert (r1.status, r2.status, r3.status) == (
        VoteStatus.ACCEPTED, VoteStatus.DOUBLE_VOTE, VoteStatus.DUPLICATE_RETRANSMIT
    )
    assert len(kernel.votes_of("carol")) == 2  # m1 and m2
    assert kernel.stats.double_vote == 1 and kernel.stats.duplicate_retransmit == 1


def test_evidence_dedup_regardless_of_arrival_order(manifest, domain, registry):
    steps_n = manifest["scenarios"]["S3_surround_nested"]
    steps_r = manifest["scenarios"]["S3b_surround_reverse_order"]
    k1 = SlashingKernel(domain=domain, registry=registry)
    k2 = SlashingKernel(domain=domain, registry=registry)
    e1 = None
    for s in steps_n:
        e1 = k1.ingest(SignedVote.from_json_dict(s["signed"])).evidence or e1
    for s in steps_r:
        k2.ingest(SignedVote.from_json_dict(s["signed"]))
    ids1 = sorted(e.evidence_id for e in k1.evidence())
    ids2 = sorted(e.evidence_id for e in k2.evidence())
    assert ids1 == ids2 and len(ids1) == 1


def test_identical_predicate_exact():
    from localffg.models import Vote
    a = SignedVote(Vote("c", "v", 0, 1, b"r"), b"k" * 32, b"s" * 64)
    b = SignedVote(Vote("c", "v", 0, 1, b"r"), b"k" * 32, b"s" * 64)
    c = SignedVote(Vote("c", "v", 0, 1, b"r2"), b"k" * 32, b"s" * 64)
    assert votes_identical(a, b)
    assert not votes_identical(a, c)
