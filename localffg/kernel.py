"""Chain voting-state kernel.

Explicit simplified finalty rule (Casper-FFG style, local definition):

  * A vote is a signed justification link  s -> t  (source_round,
    target_round) over ``block_root``, with s < t.
  * The same validator signing two DISTINCT links that satisfy either rule
    below is slashable:

      DOUBLE VOTE — same target_round, different vote:
          t1 == t2  and  vote1 != vote2
      (different source, or different block_root; byte-identical
      retransmissions of the exact same vote are DUPLICATE_RETRANSMIT and are
      explicitly NOT an offense.)

      SURROUND — one justification interval strictly nests the other:
          s2 < s1 < t1 < t2   (vote 1 is surrounded by vote 2)
      Equality on any boundary is NOT a surround (no offense under that
      rule), and crossing/partial-overlap pairs are also not surround.

  * Only votes that (a) parse, (b) carry a valid Ed25519 signature from the
    validator's registered key, (c) bind the configured chain domain and
    chain id, and (d) are cast by a validator that is an ACTIVE member at the
    vote's target epoch can ever produce slashing evidence. An unsigned /
    invalid message is rejected with an exact reason and never creates an
    offense marker.

  * Slashing weight is read from the epoch snapshot of the EARLIER of the two
    target rounds (the historical snapshot the offense is judged against).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .crypto import verify_signed_vote
from .encoding import canonical_evidence_bundle, evidence_id
from .epochs import ValidatorRegistry
from .models import (
    Evidence,
    MAX_BLOCK_ROOT_LEN,
    MAX_CHAIN_ID_LEN,
    MAX_ROUND,
    MAX_VALIDATOR_ID_LEN,
    SignedVote,
    ViolationKind,
    VoteStatus,
)


@dataclass(frozen=True)
class Conflict:
    kind: ViolationKind
    other: SignedVote


@dataclass
class IngestResult:
    status: VoteStatus
    reason: str
    evidence: list[Evidence] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)

    @property
    def slashable(self) -> bool:
        return bool(self.evidence)


def votes_identical(a: SignedVote, b: SignedVote) -> bool:
    """Exact retransmission: same validator and identical (s, t, block_root).
    Signature/pubkey must also match (a different signature for identical
    fields is still the same vote, but we require full envelope equality for
    'retransmit' semantics)."""
    va, vb = a.vote, b.vote
    return (
        va.validator_id == vb.validator_id
        and va.source_round == vb.source_round
        and va.target_round == vb.target_round
        and va.block_root == vb.block_root
        and a.signature == b.signature
        and a.signer_pubkey == b.signer_pubkey
    )


def is_double_vote(a: SignedVote, b: SignedVote) -> bool:
    va, vb = a.vote, b.vote
    return (
        va.validator_id == vb.validator_id
        and va.target_round == vb.target_round
        and not votes_identical(a, b)
    )


def is_surround(a: SignedVote, b: SignedVote) -> tuple[bool, SignedVote | None, SignedVote | None]:
    """Return (surround?, inner_vote, outer_vote).

    Strict nesting only: inner [s1,t1] strictly inside outer [s2,t2] means
    s_outer < s_inner < t_inner < t_outer.
    """
    va, vb = a.vote, b.vote
    if va.validator_id != vb.validator_id:
        return False, None, None
    s1, t1, s2, t2 = va.source_round, va.target_round, vb.source_round, vb.target_round
    if s2 < s1 < t1 < t2:
        return True, a, b
    if s1 < s2 < t2 < t1:
        return True, b, a
    return False, None, None


@dataclass
class KernelStats:
    accepted: int = 0
    duplicate_retransmit: int = 0
    double_vote: int = 0
    surround_vote: int = 0
    invalid_signature: int = 0
    invalid_chain: int = 0
    invalid_rounds: int = 0
    invalid_membership: int = 0
    unknown_validator: int = 0
    malformed: int = 0

    def as_dict(self) -> dict:
        return {
            "accepted": self.accepted,
            "duplicate_retransmit": self.duplicate_retransmit,
            "double_vote": self.double_vote,
            "surround_vote": self.surround_vote,
            "invalid_signature": self.invalid_signature,
            "invalid_chain": self.invalid_chain,
            "invalid_rounds": self.invalid_rounds,
            "invalid_membership": self.invalid_membership,
            "unknown_validator": self.unknown_validator,
            "malformed": self.malformed,
        }


class SlashingKernel:
    """Pure in-memory chain voting state + offense classification."""

    def __init__(self, *, domain: bytes, registry: ValidatorRegistry):
        self.domain = bytes(domain)
        self.registry = registry
        self.chain_id = registry.chain_id
        # validator_id -> list of accepted signed votes, in arrival order
        self._votes: dict[str, list[SignedVote]] = {}
        # evidence_id -> Evidence
        self._evidence: dict[str, Evidence] = {}
        self.stats = KernelStats()

    # -- read state ------------------------------------------------------- #

    def votes_of(self, validator_id: str) -> list[SignedVote]:
        return list(self._votes.get(validator_id, ()))

    def all_votes(self) -> list[SignedVote]:
        out: list[SignedVote] = []
        for vs in self._votes.values():
            out.extend(vs)
        return out

    def evidence(self) -> list[Evidence]:
        return list(self._evidence.values())

    def has_evidence(self, evidence_id_: str) -> bool:
        return evidence_id_ in self._evidence

    # -- ingest ----------------------------------------------------------- #

    def ingest(self, signed: SignedVote) -> IngestResult:
        v = signed.vote

        # 0) structural validation (envelope types/lengths)
        malformed = self._check_shape(signed)
        if malformed:
            self.stats.malformed += 1
            return IngestResult(VoteStatus.MALFORMED, malformed)

        # 1) rounds well-formed: 0 <= s < t, bounded
        if v.source_round < 0 or v.target_round < 0 or v.source_round >= v.target_round:
            self.stats.invalid_rounds += 1
            return IngestResult(
                VoteStatus.INVALID_ROUNDS,
                f"require 0 <= source < target; got {v.source_round}->{v.target_round}",
            )
        if v.target_round > MAX_ROUND:
            self.stats.invalid_rounds += 1
            return IngestResult(VoteStatus.INVALID_ROUNDS, f"target_round > {MAX_ROUND}")

        # 2) chain binding
        if v.chain_id != self.chain_id:
            self.stats.invalid_chain += 1
            return IngestResult(
                VoteStatus.INVALID_CHAIN,
                f"chain_id {v.chain_id!r} != kernel chain {self.chain_id!r}",
            )

        # 3) known validator
        if not self.registry.exists(v.validator_id):
            self.stats.unknown_validator += 1
            return IngestResult(
                VoteStatus.UNKNOWN_VALIDATOR,
                f"validator {v.validator_id!r} not in registry",
            )

        # 4) membership at the TARGET epoch (vote is cast for that epoch)
        target_epoch = self.registry.epoch_of_round(v.target_round)
        target_weight = self.registry.weight_at_epoch(v.validator_id, target_epoch)
        if not target_weight or target_weight <= 0:
            self.stats.invalid_membership += 1
            return IngestResult(
                VoteStatus.INVALID_MEMBERSHIP,
                f"validator not active at target epoch {target_epoch} (weight={target_weight})",
            )

        # 5) signature against the registered key (membership alone never
        #    marks an offense — cryptographic validity is required)
        expected_pubkey = self.registry.pubkey_of(v.validator_id)
        vr = verify_signed_vote(signed, self.domain, expected_pubkey=expected_pubkey)
        if not vr.ok:
            self.stats.invalid_signature += 1
            return IngestResult(VoteStatus.INVALID_SIGNATURE, f"signature: {vr.reason}")

        # 6) conflict scan against previously accepted votes of this validator
        prior = self._votes.setdefault(v.validator_id, [])

        # an exact retransmission is never an offense even if the validator
        # already accumulated other conflicts — check identity FIRST
        if any(votes_identical(signed, other) for other in prior):
            self.stats.duplicate_retransmit += 1
            return IngestResult(
                VoteStatus.DUPLICATE_RETRANSMIT,
                "identical vote already recorded (retransmission, not an offense)",
            )

        conflicts: list[Conflict] = []
        for other in prior:
            sur, _inner, _outer = is_surround(signed, other)
            if sur:
                conflicts.append(Conflict(ViolationKind.SURROUND_VOTE, other))
            elif is_double_vote(signed, other):
                conflicts.append(Conflict(ViolationKind.DOUBLE_VOTE, other))

        # 7) classify conflicts — SURROUND > DOUBLE
        if conflicts:
            evidence = [self._build_evidence(c, signed) for c in conflicts]
            # highest-priority status: SURROUND > DOUBLE
            kinds = {c.kind for c in conflicts}
            if ViolationKind.SURROUND_VOTE in kinds:
                status = VoteStatus.SURROUND_VOTE
                reason = f"{len(conflicts)} conflicting prior vote(s): surround present"
            else:
                status = VoteStatus.DOUBLE_VOTE
                reason = f"{len(conflicts)} conflicting prior vote(s): same target"
            # a genuinely conflicting vote is still stored (it is valid and is
            # itself evidence-bearing); stats count it as the offense.
            prior.append(signed)
            if status is VoteStatus.SURROUND_VOTE:
                self.stats.surround_vote += 1
            else:
                self.stats.double_vote += 1
            return IngestResult(status, reason, evidence=evidence, conflicts=conflicts)

        # genuinely new, clean vote
        prior.append(signed)
        self.stats.accepted += 1
        return IngestResult(VoteStatus.ACCEPTED, "new vote recorded")

    # -- internals -------------------------------------------------------- #

    @staticmethod
    def _check_shape(signed: SignedVote) -> str | None:
        v = signed.vote
        if not isinstance(v.source_round, int) or isinstance(v.source_round, bool):
            return "source_round not int"
        if not isinstance(v.target_round, int) or isinstance(v.target_round, bool):
            return "target_round not int"
        if not isinstance(v.chain_id, str) or not v.chain_id or len(v.chain_id) > MAX_CHAIN_ID_LEN:
            return "bad chain_id"
        if not isinstance(v.validator_id, str) or not v.validator_id or len(v.validator_id) > MAX_VALIDATOR_ID_LEN:
            return "bad validator_id"
        if not isinstance(v.block_root, (bytes, bytearray)) or not v.block_root or len(v.block_root) > MAX_BLOCK_ROOT_LEN:
            return "bad block_root"
        if not isinstance(signed.signer_pubkey, (bytes, bytearray)) or len(signed.signer_pubkey) != 32:
            return "bad signer_pubkey"
        if not isinstance(signed.signature, (bytes, bytearray)) or len(signed.signature) != 64:
            return "bad signature"
        return None

    def _build_evidence(self, conflict: Conflict, incoming: SignedVote) -> Evidence:
        vid = incoming.vote.validator_id
        # canonical ordering for the pair: earlier target first; tie-break on
        # digest so the same pair always yields the same bundle regardless of
        # arrival order.
        a, b = incoming, conflict.other
        ta, tb = a.vote.target_round, b.vote.target_round
        if ta > tb:
            a, b = b, a
        elif ta == tb and a.signature > b.signature:
            a, b = b, a

        earlier_target_epoch = self.registry.epoch_of_round(a.vote.target_round)
        weight = self.registry.weight_at_epoch(vid, earlier_target_epoch) or 0

        kind = conflict.kind
        bundle = canonical_evidence_bundle(
            kind=kind.value,
            chain_id=self.chain_id,
            validator_id=vid,
            weight_epoch=earlier_target_epoch,
            weight=weight,
            vote_a=a.to_json_dict(),
            vote_b=b.to_json_dict(),
        )
        ev = Evidence(
            evidence_id=evidence_id(bundle),
            kind=kind,
            chain_id=self.chain_id,
            validator_id=vid,
            weight_epoch=earlier_target_epoch,
            weight=weight,
            vote_a=a,
            vote_b=b,
        )
        # dedup: same pair/kind -> same id
        self._evidence.setdefault(ev.evidence_id, ev)
        return self._evidence[ev.evidence_id]
