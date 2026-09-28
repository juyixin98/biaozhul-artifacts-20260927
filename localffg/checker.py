"""Independent evidence re-checker.

This module deliberately does NOT import `localffg.kernel`. Given an evidence
bundle and the trusted validator registry (epoch snapshots), it re-derives
every claim from scratch:

  1. recompute the evidence id from the canonical binary bundle;
  2. parse and structural-validate both signed votes;
  3. verify BOTH Ed25519 signatures directly against the embedded pubkeys,
     and confirm those pubkeys match the registry;
  4. confirm chain/domain binding and active membership at BOTH target epochs;
  5. re-derive whether the pair is actually a double vote or a surround
     (exact predicates duplicated here on purpose — independent oracle);
  6. recompute the slashing weight from the earlier-target epoch snapshot.

A bundle that fails any step is REJECTED with an exact failure category —
never reported as valid. This is what makes evidence independently
re-checkable and ensures an unsigned/tampered message can never be accepted
as a penalty proof.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .crypto import canonical_payload_for
from .encoding import canonical_evidence_bundle, evidence_id
from .epochs import ValidatorRegistry
from .models import Evidence, SignedVote, ViolationKind


class CheckVerdict(str, Enum):
    VALID = "valid"
    INVALID = "invalid"      # deterministic, explainable rejection
    ERROR = "error"          # unexpected processing failure (never "valid")


@dataclass
class CheckReport:
    verdict: CheckVerdict
    evidence_id: str | None = None
    claimed_kind: str | None = None
    derived_kind: str | None = None
    validator_id: str | None = None
    weight_epoch: int | None = None
    claimed_weight: int | None = None
    derived_weight: int | None = None
    signature_a_ok: bool | None = None
    signature_b_ok: bool | None = None
    failures: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "verdict": self.verdict.value,
            "evidence_id": self.evidence_id,
            "claimed_kind": self.claimed_kind,
            "derived_kind": self.derived_kind,
            "validator_id": self.validator_id,
            "weight_epoch": self.weight_epoch,
            "claimed_weight": self.claimed_weight,
            "derived_weight": self.derived_weight,
            "signature_a_ok": self.signature_a_ok,
            "signature_b_ok": self.signature_b_ok,
            "failures": self.failures,
            "steps": self.steps,
        }


# -- independent predicates (no kernel import) ----------------------------- #

def _same_vote(a: SignedVote, b: SignedVote) -> bool:
    va, vb = a.vote, b.vote
    return (
        va.validator_id == vb.validator_id
        and va.source_round == vb.source_round
        and va.target_round == vb.target_round
        and va.block_root == vb.block_root
        and a.signature == b.signature
        and a.signer_pubkey == b.signer_pubkey
    )


def derive_violation(a: SignedVote, b: SignedVote) -> ViolationKind | None:
    va, vb = a.vote, b.vote
    if va.validator_id != vb.validator_id:
        return None
    s1, t1, s2, t2 = va.source_round, va.target_round, vb.source_round, vb.target_round
    if not (0 <= s1 < t1 and 0 <= s2 < t2):
        return None
    # strict surround either direction
    if (s2 < s1 < t1 < t2) or (s1 < s2 < t2 < t1):
        return ViolationKind.SURROUND_VOTE
    if t1 == t2 and not _same_vote(a, b):
        return ViolationKind.DOUBLE_VOTE
    return None


def _verify_one(signed: SignedVote, domain: bytes, expected_pubkey: bytes, label: str, report: CheckReport) -> bool:
    v = signed.vote
    if len(signed.signer_pubkey) != 32:
        report.failures.append(f"{label}: pubkey_length!=32")
        return False
    if len(signed.signature) != 64:
        report.failures.append(f"{label}: signature_length!=64")
        return False
    if signed.signer_pubkey != expected_pubkey:
        report.failures.append(f"{label}: pubkey does not match registry for {v.validator_id}")
        return False
    try:
        payload = canonical_payload_for(signed, domain)
        Ed25519PublicKey.from_public_bytes(signed.signer_pubkey).verify(signed.signature, payload)
    except InvalidSignature:
        report.failures.append(f"{label}: Ed25519 signature invalid")
        return False
    except Exception as exc:
        report.failures.append(f"{label}: verify error {type(exc).__name__}: {exc}")
        return False
    report.steps.append(f"{label}: Ed25519 signature valid over canonical payload")
    return True


def recheck_evidence(
    evidence_dict: dict,
    registry: ValidatorRegistry,
    *,
    domain: bytes,
) -> CheckReport:
    """Full independent verification of one evidence bundle."""
    report = CheckReport(verdict=CheckVerdict.VALID)
    try:
        ev = Evidence.from_json_dict(evidence_dict)
    except Exception as exc:
        report.verdict = CheckVerdict.INVALID
        report.failures.append(f"parse: {type(exc).__name__}: {exc}")
        return report

    report.evidence_id = ev.evidence_id
    report.claimed_kind = ev.kind.value
    report.validator_id = ev.validator_id
    report.weight_epoch = ev.weight_epoch
    report.claimed_weight = ev.weight

    # 1) evidence id recomputation
    bundle = canonical_evidence_bundle(
        kind=ev.kind.value,
        chain_id=ev.chain_id,
        validator_id=ev.validator_id,
        weight_epoch=ev.weight_epoch,
        weight=ev.weight,
        vote_a=ev.vote_a.to_json_dict(),
        vote_b=ev.vote_b.to_json_dict(),
    )
    recomputed_id = evidence_id(bundle)
    if recomputed_id != ev.evidence_id:
        report.failures.append(f"evidence_id mismatch: claimed {ev.evidence_id} != recomputed {recomputed_id}")
    else:
        report.steps.append("evidence_id matches sha256 of canonical bundle")

    va, vb = ev.vote_a.vote, ev.vote_b.vote

    # 2) chain binding for both votes and the envelope
    for label, vote in (("vote_a", va), ("vote_b", vb)):
        if vote.chain_id != registry.chain_id or ev.chain_id != registry.chain_id:
            report.failures.append(f"{label}: chain_id mismatch (vote/evidence vs registry)")
        if vote.validator_id != ev.validator_id:
            report.failures.append(f"{label}: validator_id differs from evidence subject")

    # 3) known validator + pubkey matches registry
    pubkey = registry.pubkey_of(ev.validator_id)
    if pubkey is None:
        report.failures.append(f"validator {ev.validator_id!r} unknown to registry")
    else:
        # 4) signatures (independent Ed25519 verification)
        report.signature_a_ok = _verify_one(ev.vote_a, domain, pubkey, "vote_a", report)
        report.signature_b_ok = _verify_one(ev.vote_b, domain, pubkey, "vote_b", report)

    # 5) active membership at BOTH target epochs
    epoch_a = registry.epoch_of_round(va.target_round)
    epoch_b = registry.epoch_of_round(vb.target_round)
    for label, vote, ep in (("vote_a", va, epoch_a), ("vote_b", vb, epoch_b)):
        w = registry.weight_at_epoch(ev.validator_id, ep)
        if not w or w <= 0:
            report.failures.append(f"{label}: validator not active at target epoch {ep} (weight={w})")
        report.steps.append(f"{label}: target round {vote.target_round} -> epoch {ep}, weight={w}")

    # 6) rounds
    for label, vote in (("vote_a", va), ("vote_b", vb)):
        if not (0 <= vote.source_round < vote.target_round):
            report.failures.append(f"{label}: bad rounds {vote.source_round}->{vote.target_round}")

    # 7) re-derive the violation kind
    if not _same_vote(ev.vote_a, ev.vote_b):
        derived = derive_violation(ev.vote_a, ev.vote_b)
        report.derived_kind = derived.value if derived else None
        if derived is None:
            report.failures.append("pair is neither a double vote nor a strict surround under exact predicates")
        elif derived != ev.kind:
            report.failures.append(f"claimed kind {ev.kind.value} but independently derived {derived.value}")
        else:
            report.steps.append(f"conflict re-derived independently: {derived.value}")
    else:
        report.failures.append("vote_a and vote_b are identical — retransmissions are not slashable")

    # 8) recompute weight from earlier-target epoch snapshot
    earlier_target = min(va.target_round, vb.target_round)
    derived_epoch = registry.epoch_of_round(earlier_target)
    derived_weight = registry.weight_at_epoch(ev.validator_id, derived_epoch) or 0
    report.derived_weight = derived_weight
    if derived_epoch != ev.weight_epoch:
        report.failures.append(f"weight_epoch {ev.weight_epoch} != recomputed {derived_epoch}")
    if derived_weight != ev.weight:
        report.failures.append(f"weight {ev.weight} != registry snapshot weight {derived_weight} at epoch {derived_epoch}")
    else:
        report.steps.append(f"slashing weight {derived_weight} confirmed at epoch {derived_epoch}")

    if report.failures:
        report.verdict = CheckVerdict.INVALID
    return report
