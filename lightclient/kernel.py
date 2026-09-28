"""Chain state kernel: the trust/verification core.

All protocol safety rules live here. The contract with the rest of the
system is:

  * ``bootstrap`` installs trust exactly once, from a signed checkpoint
    verified against a pinned out-of-band key;
  * ``apply_header`` accepts a header only if it extends the *current
    trusted tip* under a threshold certificate from the *currently
    authorized* committee;
  * **every rejection happens before any write.** A rejected call leaves
    the trusted tip, pending committee and all stored rows unchanged;
  * every attempt (accept or reject) is written to the audit log with a
    run id, the key intermediate state and the reason for the decision.

Epoch/rotation model (simplified):
  * a committee signs headers of its own epoch;
  * a header carrying ``next_committee`` announces the committee for
    ``epoch + 1`` in advance — the change is authorized by the current
    committee's threshold certificate on that same header;
  * the first header of epoch ``e+1`` must be signed by the committee that
    was announced during epoch ``e`` (else COMMITTEE_UNKNOWN);
  * signing a new-era header with the previous-era key set is rejected as
    STALE_COMMITTEE.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from . import codec
from .config import LightClientConfig
from .crypto import CertificateEvaluation, verify_certificate, verify_checkpoint_envelope
from .errors import (
    AlreadyInitialized,
    ChainMismatch,
    CommitteeBadTransition,
    CommitteeUnknown,
    ConflictingHeader,
    LightClientError,
    NeedCheckpoint,
    NotInitialized,
    ParentUnknown,
    ResourceLimit,
    RoundNotMonotonic,
    StaleCommittee,
    TimestampNotMonotonic,
    UntrustedBranch,
)
from .store import (
    META_CHAIN_ID,
    META_PENDING_CID,
    META_PENDING_EPOCH,
    META_TIP_DIGEST,
    META_TIP_EPOCH,
    META_TIP_HEIGHT,
    META_TIP_ROUND,
    META_TIP_TS,
    META_TRUST_PERIOD,
    Store,
)
from .types import (
    Certificate,
    CheckpointEnvelope,
    Committee,
    Header,
)

ACCEPTED = "accepted"
REJECTED = "rejected"
BOOTSTRAPPED = "bootstrapped"


@dataclass(frozen=True)
class TipInfo:
    digest: bytes
    height: int
    round: int
    epoch: int
    timestamp: int
    pending_committee_id: bytes | None
    pending_epoch: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "digest": codec.hex_(self.digest),
            "height": self.height,
            "round": self.round,
            "epoch": self.epoch,
            "timestamp": self.timestamp,
            "pending_committee_id": (
                codec.hex_(self.pending_committee_id)
                if self.pending_committee_id
                else None
            ),
            "pending_epoch": self.pending_epoch,
        }


@dataclass(frozen=True)
class ApplyResult:
    decision: str
    digest: bytes
    height: int
    epoch: int
    certificate: CertificateEvaluation
    tip: TipInfo

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "digest": codec.hex_(self.digest),
            "height": self.height,
            "epoch": self.epoch,
            "certificate": self.certificate.to_detail(),
            "tip": self.tip.to_dict(),
        }


class LightClientKernel:
    def __init__(
        self,
        store: Store,
        config: LightClientConfig,
        trusted_checkpoint_key: bytes,
        *,
        clock: Any = time.time,
        run_id: str | None = None,
    ) -> None:
        if len(trusted_checkpoint_key) != 32:
            raise ValueError("trusted_checkpoint_key must be 32 bytes")
        self.store = store
        self.config = config
        self.trusted_checkpoint_key = bytes(trusted_checkpoint_key)
        self._clock = clock
        self.run_id = run_id

    # ------------------------------------------------------------ state

    def is_initialized(self) -> bool:
        return self.store.is_initialized()

    def tip(self) -> TipInfo:
        self._require_initialized()
        m = self.store.snapshot_meta()
        pending_hex = m.get(META_PENDING_CID)
        pending_epoch = m.get(META_PENDING_EPOCH)
        return TipInfo(
            digest=codec.unhex(m[META_TIP_DIGEST], "tip_digest", 32),
            height=int(m[META_TIP_HEIGHT]),
            round=int(m[META_TIP_ROUND]),
            epoch=int(m[META_TIP_EPOCH]),
            timestamp=int(m[META_TIP_TS]),
            pending_committee_id=codec.unhex(pending_hex, "pending_cid", 32)
            if pending_hex
            else None,
            pending_epoch=int(pending_epoch) if pending_epoch is not None else None,
        )

    def chain_id(self) -> str:
        self._require_initialized()
        return str(self.store.meta_get(META_CHAIN_ID))

    def trust_period(self) -> int:
        self._require_initialized()
        return int(self.store.meta_get(META_TRUST_PERIOD))

    def required_committee_for(self, epoch: int) -> Committee | None:
        """The committee the kernel currently believes may sign ``epoch``."""
        tip = self.tip()
        if epoch == tip.epoch:
            return self.store.get_committee_for_epoch(epoch)
        if epoch == tip.epoch + 1:
            if tip.pending_committee_id is None:
                return None
            c = self.store.get_committee(tip.pending_committee_id)
            if c is not None and c.epoch != epoch:
                return None
            return c
        if epoch < tip.epoch:
            # Old-era key material may still be in storage.
            return self.store.get_committee_for_epoch(epoch)
        return None  # epochs may not be skipped

    def _require_initialized(self) -> None:
        if not self.store.is_initialized():
            raise NotInitialized("light client has no trusted checkpoint yet")

    # ------------------------------------------------------------ audit

    def _audit(
        self,
        action: str,
        result: str,
        detail: dict[str, Any],
        error: LightClientError | None = None,
    ) -> None:
        self.store.add_audit(
            run_id=self.run_id,
            at_unix=float(self._clock()),
            action=action,
            result=result,
            error_code=error.code.value if error else None,
            error_category=error.category.value if error else None,
            detail=detail,
        )

    def _tip_snapshot(self) -> dict[str, Any] | None:
        if not self.store.is_initialized():
            return None
        return self.tip().to_dict()

    def _reject(
        self,
        action: str,
        error: LightClientError,
        intermediate: dict[str, Any],
    ) -> None:
        """Record a rejection. State is guaranteed untouched by the caller."""
        detail = {
            "reason": error.reason,
            "intermediate": intermediate,
            "tip_before": self._tip_snapshot(),
            "tip_after": self._tip_snapshot(),
            "state_unchanged": True,
            **error.detail,
        }
        self._audit(action, REJECTED, detail, error)

    # --------------------------------------------------------- bootstrap

    def bootstrap(
        self, envelope: CheckpointEnvelope, *, run_id: str | None = None
    ) -> TipInfo:
        """Install the initial trusted root from a signed checkpoint."""
        old_run = self.run_id
        if run_id is not None:
            self.run_id = run_id
        action = "bootstrap"
        try:
            if self.store.is_initialized():
                tip = self.tip()
                err = AlreadyInitialized(
                    "trusted root already installed; headers cannot re-root "
                    "the client; supply a fresh process/checkpoint instead",
                    {"existing_tip": tip.to_dict()},
                )
                self._reject(action, err, {"checkpoint": envelope.checkpoint.header.height})
                raise err

            # 1. out-of-band signature against the pinned trusted key
            verify_checkpoint_envelope(envelope, self.trusted_checkpoint_key)

            cp = envelope.checkpoint
            intermediate = {
                "checkpoint_height": cp.header.height,
                "checkpoint_epoch": cp.header.epoch,
                "committee_epoch": cp.committee.epoch,
                "trust_period_seconds": cp.trust_period_seconds,
            }

            # 2. chain binding
            if cp.chain_id != self.config.chain_id or cp.header.chain_id != cp.chain_id:
                err = ChainMismatch(
                    "checkpoint belongs to a different chain",
                    {
                        "expected_chain_id": self.config.chain_id,
                        "got_chain_id": cp.chain_id,
                    },
                )
                self._reject(action, err, intermediate)
                raise err

            # 3. epoch coherence
            if cp.committee.epoch != cp.header.epoch:
                err = CommitteeBadTransition(
                    "checkpoint committee epoch must match header epoch",
                    {
                        "header_epoch": cp.header.epoch,
                        "committee_epoch": cp.committee.epoch,
                    },
                )
                self._reject(action, err, intermediate)
                raise err

            # 4. threshold policy coherence
            if cp.committee.quorum_weight != self.config.quorum_weight:
                err = CommitteeBadTransition(
                    "checkpoint committee quorum differs from configured policy",
                    {
                        "checkpoint_quorum": cp.committee.quorum_weight,
                        "configured_quorum": self.config.quorum_weight,
                    },
                )
                self._reject(action, err, intermediate)
                raise err
            if cp.committee.quorum_weight > cp.committee.total_weight:
                err = CommitteeBadTransition(
                    "checkpoint committee can never reach its own quorum",
                    {
                        "quorum": cp.committee.quorum_weight,
                        "total_weight": cp.committee.total_weight,
                    },
                )
                self._reject(action, err, intermediate)
                raise err
            if len(cp.committee.members) > self.config.max_committee_members:
                err = ResourceLimit(
                    "committee exceeds max members",
                    {
                        "members": len(cp.committee.members),
                        "limit": self.config.max_committee_members,
                    },
                )
                self._reject(action, err, intermediate)
                raise err

            if cp.trust_period_seconds != self.config.trust_period_seconds:
                err = CommitteeBadTransition(
                    "checkpoint trust period differs from configured policy",
                    {
                        "checkpoint_trust_period": cp.trust_period_seconds,
                        "configured_trust_period": self.config.trust_period_seconds,
                    },
                )
                self._reject(action, err, intermediate)
                raise err

            # 5. persist (single transaction)
            self.store.commit_bootstrap(
                chain_id=cp.chain_id,
                header=cp.header,
                committee=cp.committee,
                trust_period_seconds=cp.trust_period_seconds,
                checkpoint_key=self.trusted_checkpoint_key,
            )
            tip = self.tip()
            self._audit(
                action,
                BOOTSTRAPPED,
                {
                    "checkpoint_height": cp.header.height,
                    "checkpoint_epoch": cp.header.epoch,
                    "tip_after": tip.to_dict(),
                    "committee_id": codec.hex_(codec.committee_id(cp.committee)),
                    "committee_total_weight": cp.committee.total_weight,
                },
            )
            return tip
        finally:
            self.run_id = old_run

    # ---------------------------------------------------- apply (bytes)

    def apply_header_wire(
        self,
        header_wire: bytes,
        cert_wire: bytes,
        *,
        run_id: str | None = None,
    ) -> ApplyResult:
        """Decode + apply. Decodes with configured bounds (size -> RESOURCE_LIMIT)."""
        action = "apply_header"
        if len(header_wire) > self.config.max_header_bytes:
            err = ResourceLimit(
                "header wire exceeds size bound",
                {"size": len(header_wire), "limit": self.config.max_header_bytes},
            )
            self._reject(action, err, {"stage": "size-check"})
            raise err
        try:
            header = codec.decode_header(
                header_wire,
                max_committee_members=self.config.max_committee_members,
            )
        except LightClientError as exc:
            self._reject(action, exc, {"stage": "decode-header", "size": len(header_wire)})
            raise
        try:
            cert = codec.decode_certificate(
                cert_wire, max_votes=self.config.max_certificate_votes
            )
        except LightClientError as exc:
            self._reject(action, exc, {"stage": "decode-certificate", "size": len(cert_wire)})
            raise
        return self.apply_header(header, cert, run_id=run_id)

    # ----------------------------------------------------- apply (obj)

    def apply_header(
        self, header: Header, cert: Certificate, *, run_id: str | None = None
    ) -> ApplyResult:
        old_run = self.run_id
        if run_id is not None:
            self.run_id = run_id
        action = "apply_header"
        try:
            self._require_initialized()
            tip = self.tip()
            digest = codec.header_digest(header)
            intermediate: dict[str, Any] = {
                "header_digest": codec.hex_(digest),
                "height": header.height,
                "round": header.round,
                "epoch": header.epoch,
                "timestamp": header.timestamp,
                "parent_digest": codec.hex_(header.parent_digest),
                "tip": tip.to_dict(),
            }

            def reject(err: LightClientError, stage: str) -> None:
                intermediate["stage"] = stage
                self._reject(action, err, intermediate)

            # --- chain binding
            if header.chain_id != self.chain_id():
                err = ChainMismatch(
                    "header belongs to a different chain",
                    {
                        "expected_chain_id": self.chain_id(),
                        "got_chain_id": header.chain_id,
                    },
                )
                reject(err, "chain-id")
                raise err

            # --- height rules: must extend the trusted tip by exactly one
            existing = self.store.get_header(digest)
            if header.height < tip.height or (
                header.height == tip.height and digest == tip.digest
            ):
                err = ConflictingHeader(
                    "header at or behind trusted tip with same digest is a "
                    "no-op/replay; tip only advances forward",
                    {
                        "tip_height": tip.height,
                        "got_height": header.height,
                    },
                )
                reject(err, "height-replay")
                raise err
            if header.height == tip.height:
                other = self.store.get_header_by_height(tip.height)
                err = ConflictingHeader(
                    "equivocation: a different header is already trusted at "
                    "this height",
                    {
                        "height": header.height,
                        "trusted_digest": codec.hex_(tip.digest),
                        "got_digest": codec.hex_(digest),
                        "other_at_height": codec.hex_(codec.header_digest(other))
                        if other
                        else None,
                    },
                )
                reject(err, "height-equivocation")
                raise err
            if header.height > tip.height + 1:
                err = UntrustedBranch(
                    "header does not extend the trusted tip (height gap); "
                    "missing intermediate headers",
                    {
                        "tip_height": tip.height,
                        "got_height": header.height,
                    },
                )
                reject(err, "height-gap")
                raise err

            # --- parent link must connect to the trusted tip
            if header.parent_digest != tip.digest:
                if self.store.has_header(header.parent_digest):
                    err = ConflictingHeader(
                        "parent is known but is not the trusted tip: refusing "
                        "to switch to an untrusted branch",
                        {
                            "parent_digest": codec.hex_(header.parent_digest),
                            "trusted_tip": codec.hex_(tip.digest),
                        },
                    )
                    reject(err, "parent-fork")
                    raise err
                err = ParentUnknown(
                    "parent header is not on the trusted chain",
                    {"parent_digest": codec.hex_(header.parent_digest)},
                )
                reject(err, "parent-unknown")
                raise err

            # Safety net: never re-root from a non-tip branch even if stored.
            if existing is not None and digest != tip.digest:
                err = ConflictingHeader(
                    "header digest already stored on a side branch",
                    {"digest": codec.hex_(digest)},
                )
                reject(err, "side-branch-stored")
                raise err

            # --- round strictly monotonic
            if header.round <= tip.round:
                err = RoundNotMonotonic(
                    "round must strictly increase along the trusted chain",
                    {"tip_round": tip.round, "got_round": header.round},
                )
                reject(err, "round")
                raise err

            # --- timestamp: strictly increasing, within trust period
            if header.timestamp <= tip.timestamp:
                err = TimestampNotMonotonic(
                    "timestamp must strictly increase",
                    {"tip_timestamp": tip.timestamp, "got_timestamp": header.timestamp},
                )
                reject(err, "timestamp-order")
                raise err
            gap = header.timestamp - tip.timestamp
            trust_period = self.trust_period()
            intermediate["timestamp_gap"] = gap
            intermediate["trust_period_seconds"] = trust_period
            if gap > trust_period:
                err = NeedCheckpoint(
                    "header is beyond the trust period: a fresh trusted "
                    "checkpoint is required",
                    {
                        "tip_timestamp": tip.timestamp,
                        "got_timestamp": header.timestamp,
                        "gap_seconds": gap,
                        "trust_period_seconds": trust_period,
                    },
                )
                reject(err, "trust-period")
                raise err

            # --- epoch / committee authorization
            required = self._required_committee(header.epoch, tip, intermediate)
            intermediate["required_committee_id"] = codec.hex_(
                codec.committee_id(required)
            )

            self._detect_stale_committee(header, cert, required, tip, intermediate)

            # --- rotation announcement well-formedness
            if header.next_committee is not None:
                self._validate_announcement(header, required, tip, intermediate)

            # --- threshold signature over THIS header, by required committee
            try:
                evaluation = verify_certificate(header, cert, required)
            except LightClientError as exc:
                # crypto-layer rejections (bad signature, below quorum)
                # must also be audited with the kernel's intermediate state
                intermediate["stage"] = "certificate-verification"
                self._reject(action, exc, intermediate)
                raise
            intermediate["signed_weight"] = evaluation.signed_weight
            intermediate["participant_count"] = evaluation.participant_count

            # --- all checks passed: persist + advance tip atomically
            next_cid_hex = None
            if header.next_committee is not None:
                next_cid_hex = codec.committee_id(header.next_committee).hex()
            new_pending_cid = (
                codec.committee_id(header.next_committee)
                if header.next_committee is not None
                else None
            )
            new_pending_epoch = header.epoch + 1 if new_pending_cid else None
            self.store.commit_header(
                header=header,
                cert=cert,
                signed_weight=evaluation.signed_weight,
                participant_count=evaluation.participant_count,
                next_committee_id_hex=next_cid_hex,
                pending_cid_hex=new_pending_cid.hex() if new_pending_cid else None,
                pending_epoch=new_pending_epoch,
            )
            new_tip = self.tip()
            self._audit(
                action,
                ACCEPTED,
                {
                    **intermediate,
                    "tip_after": new_tip.to_dict(),
                    "state_unchanged": False,
                },
            )
            return ApplyResult(
                decision=ACCEPTED,
                digest=digest,
                height=header.height,
                epoch=header.epoch,
                certificate=evaluation,
                tip=new_tip,
            )
        finally:
            self.run_id = old_run

    # ----------------------------------------------------- kernel rules

    def _required_committee(
        self, epoch: int, tip: TipInfo, intermediate: dict[str, Any]
    ) -> Committee:
        if epoch == tip.epoch:
            committee = self.store.get_committee_for_epoch(epoch)
            if committee is None:  # cannot happen for the tip epoch
                err = CommitteeUnknown(
                    "no committee known for the current epoch",
                    {"epoch": epoch},
                )
                intermediate["stage"] = "committee-current"
                self._reject("apply_header", err, intermediate)
                raise err
            return committee

        if epoch == tip.epoch + 1:
            if tip.pending_committee_id is None:
                err = CommitteeUnknown(
                    "new epoch started but no committee was announced by the "
                    "previous committee",
                    {"epoch": epoch, "tip_epoch": tip.epoch},
                )
                intermediate["stage"] = "committee-announcement"
                self._reject("apply_header", err, intermediate)
                raise err
            committee = self.store.get_committee(tip.pending_committee_id)
            if committee is None or committee.epoch != epoch:
                err = CommitteeUnknown(
                    "announced committee missing or for the wrong epoch",
                    {
                        "epoch": epoch,
                        "announced_epoch": committee.epoch if committee else None,
                    },
                )
                intermediate["stage"] = "committee-announcement"
                self._reject("apply_header", err, intermediate)
                raise err
            return committee

        if epoch < tip.epoch:
            old = self.store.get_committee_for_epoch(epoch)
            err = StaleCommittee(
                "header belongs to an already-superseded epoch",
                {
                    "header_epoch": epoch,
                    "current_epoch": tip.epoch,
                    "old_committee_stored": old is not None,
                },
            )
            intermediate["stage"] = "committee-stale-epoch"
            self._reject("apply_header", err, intermediate)
            raise err

        err = CommitteeUnknown(
            "epoch skipped; committees may only advance one epoch at a time",
            {"header_epoch": epoch, "current_epoch": tip.epoch},
        )
        intermediate["stage"] = "committee-skipped"
        self._reject("apply_header", err, intermediate)
        raise err

    def _detect_stale_committee(
        self,
        header: Header,
        cert: Certificate,
        required: Committee,
        tip: TipInfo,
        intermediate: dict[str, Any],
    ) -> None:
        """If the votes are all from a previous-era committee, classify that
        explicitly as STALE_COMMITTEE instead of a generic bad signature."""
        signers = {v.signer for v in cert.votes}
        required_keys = {m.public_key for m in required.members}
        if signers and signers.issubset(required_keys):
            return  # possibly a valid current-committee certificate

        best: tuple[int, Committee] | None = None
        # Candidate previous-era committees: the tip-epoch committee when a
        # new epoch has begun, plus any stored older committees.
        candidates: list[Committee] = []
        for cid_hex in self.store.all_committee_ids():
            c = self.store.get_committee(codec.unhex(cid_hex, "committee_id", 32))
            if c is not None and c.epoch < required.epoch:
                candidates.append(c)
        for c in candidates:
            keys = {m.public_key for m in c.members}
            overlap = len(signers & keys)
            if best is None or overlap > best[0]:
                best = (overlap, c)

        if best is not None and best[0] > 0 and best[0] == len(signers):
            err = StaleCommittee(
                "new-era header signed by the previous-era committee",
                {
                    "header_epoch": header.epoch,
                    "required_epoch": required.epoch,
                    "signer_epoch": best[1].epoch,
                    "matching_signers": best[0],
                },
            )
            intermediate["stage"] = "stale-committee"
            self._reject("apply_header", err, intermediate)
            raise err
        # Otherwise let verify_certificate emit the precise signature error.

    def _validate_announcement(
        self,
        header: Header,
        required: Committee,
        tip: TipInfo,
        intermediate: dict[str, Any],
    ) -> None:
        nc = header.next_committee
        assert nc is not None
        if nc.epoch != header.epoch + 1:
            err = CommitteeBadTransition(
                "announced committee must be for exactly epoch + 1",
                {"header_epoch": header.epoch, "announced_epoch": nc.epoch},
            )
            intermediate["stage"] = "announcement-epoch"
            self._reject("apply_header", err, intermediate)
            raise err
        if nc.quorum_weight != self.config.quorum_weight:
            err = CommitteeBadTransition(
                "announced committee quorum differs from configured policy",
                {
                    "announced_quorum": nc.quorum_weight,
                    "configured_quorum": self.config.quorum_weight,
                },
            )
            intermediate["stage"] = "announcement-quorum"
            self._reject("apply_header", err, intermediate)
            raise err
        if nc.quorum_weight > nc.total_weight:
            err = CommitteeBadTransition(
                "announced committee can never reach its own quorum",
                {"quorum": nc.quorum_weight, "total_weight": nc.total_weight},
            )
            intermediate["stage"] = "announcement-unreachable"
            self._reject("apply_header", err, intermediate)
            raise err
        if len(nc.members) > self.config.max_committee_members:
            err = ResourceLimit(
                "announced committee exceeds max members",
                {"members": len(nc.members), "limit": self.config.max_committee_members},
            )
            intermediate["stage"] = "announcement-size"
            self._reject("apply_header", err, intermediate)
            raise err
        # The signers authorizing THIS header must be the current committee
        # (checked via verify_certificate); the announcement rides on that
        # certificate. Distinct public keys only.
        keys = [m.public_key for m in nc.members]
        if len(set(keys)) != len(keys):
            err = CommitteeBadTransition(
                "announced committee contains duplicate member keys"
            )
            intermediate["stage"] = "announcement-duplicate"
            self._reject("apply_header", err, intermediate)
            raise err
