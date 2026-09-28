"""Ingest pipeline: the component that ties all layers together.

Order of checks on every input:
    1. parse shape      -> malformed input never becomes a vote
    2. chain domain    -> wrong chain id rejected
    3. epoch ordering  -> source < target required
    4. signature       -> unsigned/forged input can never create an offense
    5. membership      -> validator must be in the target-epoch snapshot
    6. de-duplication  -> identical re-send is a duplicate, not a violation
    7. conflict scan   -> double vote / surround vote over PRIOR valid votes
    8. finality fold   -> only verified member votes move the finality cursor

Penalty marks are written only when a fully verified evidence packet exists.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .crypto import verify_vote
from .encoding import EncodingError, vote_message_root
from .evidence import (
    Evidence,
    build_evidence,
    classify_conflict,
    surround_pair,
    votes_identical,
)
from .models import IngestStatus, Offense, RejectReason, Vote
from .registry import ValidatorRegistry
from .state import Checkpoint, FinalityState
from .storage import Storage


@dataclass
class IngestResult:
    status: IngestStatus
    seq: int | None = None
    reject_reason: RejectReason | None = None
    message_root: bytes | None = None
    evidences: list[Evidence] = field(default_factory=list)
    link_weight: int | None = None
    total_weight: int | None = None
    quorum: bool = False
    justified: Checkpoint | None = None
    finalized: Checkpoint | None = None

    def as_dict(self) -> dict:
        return {
            "status": self.status.value,
            "seq": self.seq,
            "reject_reason": self.reject_reason.value if self.reject_reason else None,
            "message_root": self.message_root.hex() if self.message_root else None,
            "evidences": [e.packet for e in self.evidences],
            "finality": {
                "link_weight": self.link_weight,
                "total_weight": self.total_weight,
                "quorum": self.quorum,
                "justified": self.justified.as_dict() if self.justified else None,
                "finalized": self.finalized.as_dict() if self.finalized else None,
            },
        }


@dataclass
class Counters:
    received: int = 0
    accepted: int = 0
    duplicate: int = 0
    rejected: dict[str, int] = field(default_factory=dict)
    # invalid signatures are a rejection subset but tracked separately too.
    invalid_signatures: int = 0
    # genuinely verified offenses, keyed by validator pubkey hex.
    offenses: dict[str, dict[str, int]] = field(default_factory=dict)
    evidences_created: int = 0

    def bump_reject(self, reason: RejectReason) -> None:
        self.rejected[reason.value] = self.rejected.get(reason.value, 0) + 1
        if reason is RejectReason.INVALID_SIGNATURE:
            self.invalid_signatures += 1

    def bump_offense(self, pubkey: bytes, offense: Offense) -> None:
        bucket = self.offenses.setdefault(pubkey.hex(), {})
        bucket[offense.value] = bucket.get(offense.value, 0) + 1

    def as_dict(self) -> dict:
        return {
            "received": self.received,
            "accepted": self.accepted,
            "duplicate": self.duplicate,
            "rejected_by_reason": dict(sorted(self.rejected.items())),
            "invalid_signatures": self.invalid_signatures,
            "offenses_by_validator": {k: dict(sorted(v.items()))
                                      for k, v in sorted(self.offenses.items())},
            "evidences_created": self.evidences_created,
            "real_conflicts_total": sum(sum(v.values()) for v in self.offenses.values()),
        }


class SlashingService:
    def __init__(self, chain_id: int, genesis_root: bytes,
                 registry: ValidatorRegistry, storage: Storage,
                 run_id: str, logger=None):
        if registry.chain_id != chain_id:
            raise ValueError("registry chain id mismatch")
        self.chain_id = chain_id
        self.genesis_root = bytes(genesis_root)
        self.registry = registry
        self.storage = storage
        self.run_id = run_id
        self.logger = logger
        self.counters = Counters()
        self.state = FinalityState(registry, self.genesis_root)
        # in-memory per-validator accepted votes for fast conflict scans
        self._votes: dict[bytes, list[Vote]] = {}
        self._seq = 0
        self._restore()

    # ------------------------------------------------------------ lifecycle
    def _log(self, vote: Vote | None, step: str, status: str, **extra) -> None:
        if self.logger is None:
            return
        validator = vote.validator_pubkey.hex() if vote else extra.pop("validator", None)
        self.logger.info(
            f"step {step}: {status}",
            extra={"run_id": self.run_id, "seq": self._seq or None,
                   "validator": validator, "step": step, "status": status,
                   "chain_id": self.chain_id, **extra},
        )

    def _restore(self) -> None:
        """Rebuild in-memory state and finality cursor from durable storage."""
        (j_epoch, j_root), fin = self.storage.load_checkpoints()
        if (j_epoch, j_root) != (0, self.genesis_root):
            self.state.justified = Checkpoint(j_epoch, j_root)
            self.state.justified_history.append(self.state.justified)
        if fin is not None:
            self.state.finalized = Checkpoint(*fin)
            self.state.finalized_history.append(self.state.finalized)
        for se, sr, te, tr, pk, w in self.storage.load_link_voters():
            key = (se, bytes(sr), te, bytes(tr))
            self.state.links.add(key, bytes(pk), w)
        for vote in self._all_stored_votes():
            self._votes.setdefault(vote.validator_pubkey, []).append(vote)
        self._seq = self.storage.conn.execute(
            "SELECT COALESCE(MAX(seq),0) FROM votes").fetchone()[0]
        ev_rows = self.storage.conn.execute(
            "SELECT COUNT(*) FROM evidences").fetchone()[0]
        self.counters.evidences_created = ev_rows

    def _all_stored_votes(self) -> list[Vote]:
        rows = self.storage.conn.execute(
            "SELECT * FROM votes ORDER BY id").fetchall()
        return [Storage._row_to_vote(r) for r in rows]

    # --------------------------------------------------------------- ingest
    def ingest_raw(self, payload) -> IngestResult:
        """Entry point for envelopes (dict/str/bytes). Parses then ingests."""
        self.counters.received += 1
        try:
            vote = self._parse(payload)
        except EncodingError as exc:
            self._reject(None, RejectReason.MALFORMED, str(exc))
            return IngestResult(status=IngestStatus.REJECTED,
                                reject_reason=RejectReason.MALFORMED)
        return self._ingest_parsed(vote)

    def ingest(self, vote: Vote) -> IngestResult:
        self.counters.received += 1
        try:
            vote.validate_shape()
        except EncodingError as exc:
            self._reject(vote, RejectReason.MALFORMED, str(exc))
            return IngestResult(status=IngestStatus.REJECTED,
                                reject_reason=RejectReason.MALFORMED)
        return self._ingest_parsed(vote)

    def _ingest_parsed(self, vote: Vote) -> IngestResult:

        # 2. chain domain binding
        if vote.chain_id != self.chain_id:
            self._reject(vote, RejectReason.BAD_CHAIN_ID,
                         f"vote chain_id {vote.chain_id} != {self.chain_id}")
            return IngestResult(status=IngestStatus.REJECTED,
                                reject_reason=RejectReason.BAD_CHAIN_ID)

        # 3. round ordering
        if vote.source_epoch >= vote.target_epoch:
            self._reject(vote, RejectReason.BAD_EPOCH_ORDER,
                         f"{vote.source_epoch} >= {vote.target_epoch}")
            return IngestResult(status=IngestStatus.REJECTED,
                                reject_reason=RejectReason.BAD_EPOCH_ORDER)

        # 4. cryptographic verification BEFORE any membership/conflict effect
        mroot = vote_message_root(
            source_epoch=vote.source_epoch, source_root=vote.source_root,
            target_epoch=vote.target_epoch, target_root=vote.target_root)
        valid_sig = verify_vote(
            vote.validator_pubkey, vote.signature,
            chain_id=vote.chain_id, validator_pubkey=vote.validator_pubkey,
            source_epoch=vote.source_epoch, source_root=vote.source_root,
            target_epoch=vote.target_epoch, target_root=vote.target_root)
        if not valid_sig:
            self._reject(vote, RejectReason.INVALID_SIGNATURE,
                         f"ed25519 verification failed for message_root {mroot.hex()[:16]}")
            return IngestResult(status=IngestStatus.REJECTED,
                                reject_reason=RejectReason.INVALID_SIGNATURE,
                                message_root=mroot)
        self._log(vote, "verify_signature", "ok",
                  detail=f"message_root={mroot.hex()[:16]}")

        # 5. membership against the TARGET epoch snapshot
        if not self.registry.has_snapshot(vote.target_epoch):
            self._reject(vote, RejectReason.UNKNOWN_VALIDATOR,
                         f"no snapshot for epoch {vote.target_epoch}")
            return IngestResult(status=IngestStatus.REJECTED,
                                reject_reason=RejectReason.UNKNOWN_VALIDATOR,
                                message_root=mroot)
        snapshot = self.registry.snapshot(vote.target_epoch)
        if not snapshot.is_member(vote.validator_pubkey):
            self._reject(vote, RejectReason.VALIDATOR_INACTIVE,
                         f"not in epoch {vote.target_epoch} set")
            return IngestResult(status=IngestStatus.REJECTED,
                                reject_reason=RejectReason.VALIDATOR_INACTIVE,
                                message_root=mroot)

        # 6. exact duplicate re-transmission (identical content + signature)
        prior = self._votes.get(vote.validator_pubkey, [])
        for old in prior:
            if votes_identical(old, vote):
                self.counters.duplicate += 1
                self._log(vote, "dedup", "duplicate",
                          detail="identical re-transmission, not a violation")
                self.storage.append_event(
                    self.run_id, "duplicate", vote.validator_pubkey,
                    "identical re-transmission", {"message_root": mroot.hex()})
                return IngestResult(status=IngestStatus.DUPLICATE,
                                    message_root=mroot)
        # same content but different signature is impossible for deterministic
        # Ed25519 on the same key; a different signature over the same body
        # would have failed verification above.

        # 7. conflict scan over prior VERIFIED votes only
        evidences = self._scan_conflicts(vote, prior, mroot)

        # 8. accept + fold into finality kernel
        self._seq += 1
        self.counters.accepted += 1
        self._votes.setdefault(vote.validator_pubkey, []).append(vote)
        self.storage.insert_vote(vote, mroot, self._seq)

        transition = self.state.apply_vote(
            vote.validator_pubkey, vote.source_epoch, vote.source_root,
            vote.target_epoch, vote.target_root)
        link_key = (vote.source_epoch, vote.source_root,
                    vote.target_epoch, vote.target_root)
        self.storage.add_link_voter(link_key, vote.validator_pubkey,
                                    snapshot.weight_of(vote.validator_pubkey))
        self.storage.save_checkpoints(
            (self.state.justified.epoch, self.state.justified.root),
            (self.state.finalized.epoch, self.state.finalized.root)
            if self.state.finalized else None)
        self.storage.append_event(
            self.run_id, "accepted", vote.validator_pubkey,
            f"vote accepted seq={self._seq}",
            {"message_root": mroot.hex(), "target_epoch": vote.target_epoch,
             "link_weight": transition.link_weight,
             "quorum": transition.quorum})
        self.storage.commit()

        if transition.justified:
            self._log(vote, "finality", "justified",
                      epoch=transition.justified.epoch)
        if transition.finalized:
            self._log(vote, "finality", "finalized",
                      epoch=transition.finalized.epoch)

        return IngestResult(
            status=IngestStatus.ACCEPTED, seq=self._seq, message_root=mroot,
            evidences=evidences,
            link_weight=transition.link_weight, total_weight=transition.total_weight,
            quorum=transition.quorum,
            justified=transition.justified, finalized=transition.finalized)

    # ------------------------------------------------------------- conflict
    def _scan_conflicts(self, vote: Vote, prior: list[Vote],
                        mroot: bytes) -> list[Evidence]:
        found: list[Evidence] = []
        for old in prior:
            offense = classify_conflict(old, vote)
            if offense is None:
                continue
            try:
                snap_old = self.registry.snapshot(old.target_epoch)
                snap_new = self.registry.snapshot(vote.target_epoch)
            except Exception as exc:  # missing snapshot must not silently pass
                self._log(vote, "conflict", "error",
                          detail=f"missing snapshot: {exc}")
                continue
            evidence = build_evidence(old, vote, snap_old, snap_new)
            if not self.storage.insert_evidence(
                    evidence.evidence_id, offense.value,
                    vote.validator_pubkey, evidence.packet, self._seq + 1):
                self._log(vote, "conflict", "duplicate_evidence",
                          detail=evidence.evidence_id)
                continue
            self.counters.bump_offense(vote.validator_pubkey, offense)
            self.counters.evidences_created += 1
            self._persist_slash_marks(evidence)
            self.storage.append_event(
                self.run_id, "evidence", vote.validator_pubkey,
                f"{offense.value} verified",
                {"evidence_id": evidence.evidence_id,
                 "message_root": mroot.hex()})
            self._log(vote, "conflict", offense.value,
                      offense=offense.value, evidence_id=evidence.evidence_id)
            found.append(evidence)
        return found

    def _persist_slash_marks(self, evidence: Evidence) -> None:
        """One mark per (validator, epoch); weight from that epoch's snapshot."""
        pk = bytes.fromhex(evidence.packet["validator_pubkey"])
        for item in evidence.packet["slashable_weight"]["epochs"]:
            self.storage.add_slash_mark(
                pk, item["epoch"], item["weight"],
                evidence.evidence_id, self._seq + 1)

    # ---------------------------------------------------------------- helpers
    def _parse(self, payload) -> Vote:
        # local Vote objects are accepted from internal callers/tests
        from .models import vote_from_envelope
        if isinstance(payload, Vote):
            payload.validate_shape()
            return payload
        return vote_from_envelope(payload)

    def _reject(self, vote: Vote | None, reason: RejectReason, detail: str) -> None:
        self.counters.bump_reject(reason)
        self.storage.append_event(
            self.run_id, "rejected",
            vote.validator_pubkey if vote else None,
            f"{reason.value}: {detail}",
            {"reject_reason": reason.value})
        self.storage.commit()
        self._log(vote, reason.value, "rejected",
                  reject_reason=reason.value, detail=detail)

    def stats(self) -> dict:
        slash_rows = self.storage.slash_marks()
        per_validator: dict[str, int] = {}
        for r in slash_rows:
            key = r["validator_pubkey"].hex()
            per_validator[key] = per_validator.get(key, 0) + r["weight"]
        return {
            **self.counters.as_dict(),
            "slashable_weight_by_validator": dict(sorted(per_validator.items())),
            "finality": self.state.snapshot_state(),
        }
