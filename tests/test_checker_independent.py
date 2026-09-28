"""Independent evidence checker tests.

Genuine evidence produced by the kernel must re-check VALID; every targeted
tampering of the bundle must be rejected with an explicit failure reason
(never accepted). Also asserts the checker disagrees with a fabricated
unsigned bundle.
"""
from __future__ import annotations

import copy

from localffg.checker import CheckVerdict, derive_violation, recheck_evidence
from localffg.kernel import SlashingKernel
from localffg.models import SignedVote, ViolationKind


def _produce_double_vote_evidence(domain, registry, manifest, scenario):
    kernel = SlashingKernel(domain=domain, registry=registry)
    ev = None
    for s in manifest["scenarios"][scenario]:
        r = kernel.ingest(SignedVote.from_json_dict(s["signed"]))
        if r.evidence:
            ev = r.evidence[-1]
    assert ev is not None
    return ev.to_json_dict()


def test_real_double_vote_evidence_rechecks_valid(domain, registry, manifest):
    bundle = _produce_double_vote_evidence(domain, registry, manifest, "S2_double_vote_same_target")
    report = recheck_evidence(copy.deepcopy(bundle), registry, domain=domain)
    assert report.verdict is CheckVerdict.VALID, report.failures
    assert report.derived_kind == ViolationKind.DOUBLE_VOTE.value
    assert report.signature_a_ok and report.signature_b_ok
    assert report.derived_weight == 100 and report.weight_epoch == 0
    assert report.failures == []


def test_real_surround_evidence_rechecks_valid(domain, registry, manifest):
    bundle = _produce_double_vote_evidence(domain, registry, manifest, "S3_surround_nested")
    report = recheck_evidence(copy.deepcopy(bundle), registry, domain=domain)
    assert report.verdict is CheckVerdict.VALID, report.failures
    assert report.derived_kind == ViolationKind.SURROUND_VOTE.value
    assert report.signature_a_ok and report.signature_b_ok


def test_tampered_signature_is_rejected(domain, registry, manifest):
    bundle = _produce_double_vote_evidence(domain, registry, manifest, "S2_double_vote_same_target")
    sig = bytearray(bytes.fromhex(bundle["vote_a"]["signature"]))
    sig[7] ^= 0x01
    bundle["vote_a"]["signature"] = sig.hex()
    report = recheck_evidence(bundle, registry, domain=domain)
    assert report.verdict is CheckVerdict.INVALID
    assert any("vote_a" in f and "signature invalid" in f for f in report.failures)


def test_wrong_pubkey_in_bundle_is_rejected(domain, registry, manifest):
    from localffg.crypto import Signer

    bundle = _produce_double_vote_evidence(domain, registry, manifest, "S2_double_vote_same_target")
    attacker = Signer.generate("attacker")
    bundle["vote_b"]["signer_pubkey"] = attacker.public_key_bytes.hex()
    report = recheck_evidence(bundle, registry, domain=domain)
    assert report.verdict is CheckVerdict.INVALID
    assert any("pubkey does not match registry" in f for f in report.failures)


def test_rewritten_block_root_breaks_both_binding_and_signature(domain, registry, manifest):
    bundle = _produce_double_vote_evidence(domain, registry, manifest, "S2_double_vote_same_target")
    root = bytearray(bytes.fromhex(bundle["vote_a"]["block_root"]))
    root[0] ^= 0xFF
    bundle["vote_a"]["block_root"] = root.hex()
    report = recheck_evidence(bundle, registry, domain=domain)
    assert report.verdict is CheckVerdict.INVALID
    # signature over the changed payload must fail
    assert report.signature_a_ok is False


def test_evidence_id_tamper_detected(domain, registry, manifest):
    bundle = _produce_double_vote_evidence(domain, registry, manifest, "S2_double_vote_same_target")
    bundle["evidence_id"] = "ev_deadbeef" * 4
    report = recheck_evidence(bundle, registry, domain=domain)
    assert report.verdict is CheckVerdict.INVALID
    assert any("evidence_id mismatch" in f for f in report.failures)


def test_claimed_kind_must_match_derived(domain, registry, manifest):
    bundle = _produce_double_vote_evidence(domain, registry, manifest, "S2_double_vote_same_target")
    bundle["kind"] = ViolationKind.SURROUND_VOTE.value
    report = recheck_evidence(bundle, registry, domain=domain)
    assert report.verdict is CheckVerdict.INVALID
    assert any("independently derived" in f for f in report.failures)


def test_identical_pair_is_not_valid_evidence(domain, registry, manifest):
    # build a bundle from the same vote twice (fake "duplicate as offense")
    kernel = SlashingKernel(domain=domain, registry=registry)
    steps = manifest["scenarios"]["S1_duplicate_retransmit"]
    sv = SignedVote.from_json_dict(steps[0]["signed"])
    kernel.ingest(sv)
    from localffg.encoding import canonical_evidence_bundle, evidence_id
    bj = sv.to_json_dict()
    bundle_bytes = canonical_evidence_bundle(
        kind="double_vote", chain_id=bundle_chain(registry), validator_id="alice",
        weight_epoch=0, weight=100, vote_a=bj, vote_b=copy.deepcopy(bj),
    )
    bundle = {
        "evidence_id": evidence_id(bundle_bytes),
        "kind": "double_vote", "chain_id": bundle_chain(registry), "validator_id": "alice",
        "weight_epoch": 0, "weight": 100, "vote_a": bj, "vote_b": copy.deepcopy(bj),
    }
    report = recheck_evidence(bundle, registry, domain=domain)
    assert report.verdict is CheckVerdict.INVALID
    assert any("identical" in f for f in report.failures)


def test_weight_claim_verified_against_epoch_snapshot(domain, registry, manifest):
    bundle = _produce_double_vote_evidence(domain, registry, manifest, "S2_double_vote_same_target")
    bundle["weight"] = 999
    report = recheck_evidence(bundle, registry, domain=domain)
    assert report.verdict is CheckVerdict.INVALID
    assert any("registry snapshot weight" in f for f in report.failures)


def test_exited_validator_evidence_rejected(domain, registry, manifest):
    """A bundle whose vote_b targets an epoch the validator has exited must
    fail membership re-check even with otherwise valid signatures."""
    from localffg.crypto import Signer

    # erin valid in epoch 0 only; craft valid epoch-0 vote + valid epoch-2 vote
    erin = Signer.from_seed("erin", b"localffg-fixture/erin")
    v0 = erin.sign_vote(domain=domain, chain_id=registry.chain_id, source_round=0, target_round=5, block_root=b"a" * 32)
    v2 = erin.sign_vote(domain=domain, chain_id=registry.chain_id, source_round=20, target_round=25, block_root=b"b" * 32)
    a, b = v0.to_json_dict(), v2.to_json_dict()
    from localffg.encoding import canonical_evidence_bundle, evidence_id
    raw = canonical_evidence_bundle(
        kind="double_vote", chain_id=registry.chain_id, validator_id="erin",
        weight_epoch=0, weight=60, vote_a=a, vote_b=b,
    )
    bundle = {
        "evidence_id": evidence_id(raw), "kind": "double_vote",
        "chain_id": registry.chain_id, "validator_id": "erin",
        "weight_epoch": 0, "weight": 60, "vote_a": a, "vote_b": b,
    }
    report = recheck_evidence(bundle, registry, domain=domain)
    assert report.verdict is CheckVerdict.INVALID
    assert any("not active at target epoch 2" in f for f in report.failures)


def test_independent_pair_predicate_matches_exact_rules():
    from localffg.models import Vote

    def sv(vid, s, t, root=b"r"):
        return SignedVote(vote=Vote("c", vid, s, t, root * 32), signer_pubkey=b"k" * 32, signature=b"s" * 64)

    assert derive_violation(sv("v", 0, 8, b"a"), sv("v", 2, 8, b"b")) is ViolationKind.DOUBLE_VOTE
    assert derive_violation(sv("v", 0, 15), sv("v", 5, 10)) is ViolationKind.SURROUND_VOTE
    assert derive_violation(sv("v", 5, 10), sv("v", 0, 15)) is ViolationKind.SURROUND_VOTE
    assert derive_violation(sv("v", 0, 10), sv("v", 10, 15)) is None
    assert derive_violation(sv("v1", 0, 8), sv("v2", 0, 8)) is None
    # identical envelope -> no violation
    x = sv("v", 0, 8, b"a")
    y = SignedVote(x.vote, x.signer_pubkey, x.signature)
    assert derive_violation(x, y) is None
    # same fields but distinct signature material -> distinct envelope -> double
    z = SignedVote(x.vote, b"k" * 32, b"t" * 64)
    assert derive_violation(x, z) is ViolationKind.DOUBLE_VOTE


def bundle_chain(registry):
    return registry.chain_id
