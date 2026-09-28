"""Chain state kernel: every authorization rule for accepting a header.

The kernel is the only component that mutates trusted state. All rules for one
header (or one replay batch) execute inside a single store transaction; on any
rule failure the transaction is rolled back and the trusted tip is provably
unchanged (tests snapshot the tip before/after).

Rule order (first failure wins; all attempted checks are also appended to the
report's ``trace`` so logs explain the decision):

1. parse/shape                -> INPUT
2. freshness (batch-level)    -> TRUST_EXPIRED
3. parent link / known header -> INPUT (missing) / STATE_CONFLICT (fork)
4. round monotonicity         -> STATE_CONFLICT
5. timestamp monotonicity     -> STATE_CONFLICT (backdated) / INPUT (future)
6. authorizing committee + rotation commitment binding -> INPUT
7. every signer known in the authorizing committee      -> COMPUTATION
8. every Ed25519 signature valid                        -> COMPUTATION
9. signed weight >= floor(2W/3)+1                       -> COMPUTATION
10. commit: header (and maybe committee), advance tip

Trust model summary
-------------------
* A client is bootstrapped out-of-band with a trusted ``(header, committee)``
  checkpoint. Checkpoints are asserted, never fetched from a peer.
* A header is trusted iff it extends the current trusted tip and carries a
  weight-qualified certificate of the *currently active* committee. A header
  whose parent is anything other than the tip is an untrusted branch and can
  never move the root — even if it carries a superficially valid certificate.
* A rotation (``next_committee_commitment``) takes effect only when the header
  that names it is accepted under the prior committee's threshold certificate.
  Afterwards the new committee alone authorizes successors; signatures from
  the old committee on post-rotation headers fail as ``SIGNER_UNKNOWN``.
* The client is fresh while its tip timestamp is within ``trust_period_ms`` of
  the clock. Beyond it, any update attempt returns ``TRUST_EXPIRED`` and asks
  for a new out-of-band checkpoint — long offline -> new checkpoint.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import encoding
from .clock import Clock, RunRecorder
from .config import KernelConfig
from .crypto import CertificateVerification, verify_certificate
from .errors import Code, LightClientError
from .store import Store, StoredHeader, TipState
from .types import Certificate, Checkpoint, Committee, Header


@dataclass
class AcceptReport:
    accepted: bool
    run_id: str
    root: Optional[str] = None
    round: Optional[int] = None
    parent_root: Optional[str] = None
    tip_before: Optional[Dict[str, Any]] = None
    tip_after: Optional[Dict[str, Any]] = None
    active_committee_before: Optional[str] = None
    active_committee_after: Optional[str] = None
    cert: Optional[Dict[str, Any]] = None
    trace: List[str] = field(default_factory=list)
    failure_code: Optional[str] = None
    failure_category: Optional[str] = None
    failure_reason: Optional[str] = None
    needs_new_checkpoint: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "accepted": self.accepted,
            "run_id": self.run_id,
            "root": self.root,
            "round": self.round,
            "parent_root": self.parent_root,
            "tip_before": self.tip_before,
            "tip_after": self.tip_after,
            "active_committee_before": self.active_committee_before,
            "active_committee_after": self.active_committee_after,
            "cert": self.cert,
            "trace": self.trace,
            "failure_code": self.failure_code,
            "failure_category": self.failure_category,
            "failure_reason": self.failure_reason,
            "needs_new_checkpoint": self.needs_new_checkpoint,
        }


def _tip_dict(tip: TipState) -> Dict[str, Any]:
    return {
        "tip_header_root": (
            None if tip.tip_header_root is None
            else "0x" + tip.tip_header_root.hex()
        ),
        "active_committee_commitment": (
            None
            if tip.active_committee_commitment is None
            else "0x" + tip.active_committee_commitment.hex()
        ),
        "tip_round": tip.tip_round,
        "tip_timestamp_ms": tip.tip_timestamp_ms,
        "sequence": tip.sequence,
    }


class ChainKernel:
    def __init__(
        self,
        store: Store,
        clock: Clock,
        config: Optional[KernelConfig] = None,
        recorder: Optional[RunRecorder] = None,
    ):
        self.store = store
        self.clock = clock
        self.config = config or KernelConfig()
        self.recorder = recorder or RunRecorder()

    # ------------------------------------------------------------------ #
    # Bootstrapping: out-of-band trusted checkpoint                       #
    # ------------------------------------------------------------------ #
    def install_checkpoint(self, checkpoint: Checkpoint) -> AcceptReport:
        """Assert a trusted checkpoint. Only possible before initialization."""
        root = encoding.header_root(checkpoint.header)
        commitment = encoding.committee_commitment(checkpoint.committee)
        before = self.store.get_tip()
        trace = [
            f"checkpoint header root = 0x{root.hex()[:16]}… round={checkpoint.header.round}",
            f"checkpoint committee commitment = 0x{commitment.hex()[:16]}… "
            f"members={len(checkpoint.committee.members)} "
            f"total_weight={checkpoint.committee.total_weight}",
        ]
        if before.tip_header_root is not None:
            trace.append("client already initialized; refusing to overwrite root")
            self._record(
                "checkpoint_rejected",
                reason=Code.CHECKPOINT_CONFLICT.value,
                tip_before=_tip_dict(before),
                trace=trace,
            )
            raise LightClientError(
                Code.CHECKPOINT_CONFLICT,
                "a trusted checkpoint is already installed; a client does not "
                "replace its root via the update interface (install a fresh DB)",
                details={"existing_tip": "0x" + before.tip_header_root.hex()},
                trace=trace,
            )
        with self.store.chain_txn() as conn:
            self.store.insert_header(conn, checkpoint.header, root)
            self.store.insert_committee(conn, checkpoint.committee, commitment)
            self.store.init_tip(
                conn,
                root,
                checkpoint.header.round,
                checkpoint.header.timestamp_ms,
                commitment,
            )
        after = self.store.get_tip()
        report = AcceptReport(
            accepted=True,
            run_id=self.recorder.run_id,
            root="0x" + root.hex(),
            round=checkpoint.header.round,
            parent_root="0x" + checkpoint.header.parent_root.hex(),
            tip_before=_tip_dict(before),
            tip_after=_tip_dict(after),
            active_committee_before=None,
            active_committee_after="0x" + commitment.hex(),
            trace=trace + ["checkpoint installed out-of-band (asserted, not fetched)"],
        )
        self._record("checkpoint_installed", **report.to_dict())
        return report

    # ------------------------------------------------------------------ #
    # Reads                                                               #
    # ------------------------------------------------------------------ #
    def head(self) -> Dict[str, Any]:
        tip = self.store.get_tip()
        result: Dict[str, Any] = {
            "initialized": tip.tip_header_root is not None,
            **_tip_dict(tip),
        }
        if tip.tip_header_root is not None:
            stored = self.store.get_header(tip.tip_header_root)
            result["header"] = stored.header_json if stored else None
            result["fresh"] = self.is_fresh()
        else:
            result["fresh"] = False
        return result

    def is_fresh(self, *, now_ms: Optional[int] = None) -> bool:
        tip = self.store.get_tip()
        if tip.tip_header_root is None:
            return False
        now = self.clock.now_ms() if now_ms is None else now_ms
        return now - tip.tip_timestamp_ms <= self.config.trust_period_ms

    def trust_status(self) -> Dict[str, Any]:
        tip = self.store.get_tip()
        now = self.clock.now_ms()
        if tip.tip_header_root is None:
            return {
                "initialized": False,
                "fresh": False,
                "needs_new_checkpoint": True,
            }
        age = now - tip.tip_timestamp_ms
        fresh = age <= self.config.trust_period_ms
        return {
            "initialized": True,
            "now_ms": now,
            "tip_timestamp_ms": tip.tip_timestamp_ms,
            "age_ms": age,
            "trust_period_ms": self.config.trust_period_ms,
            "fresh": fresh,
            "needs_new_checkpoint": not fresh,
        }

    # ------------------------------------------------------------------ #
    # Single-header update                                                #
    # ------------------------------------------------------------------ #
    def apply_header(
        self,
        header: Header,
        certificate: Certificate,
        next_committee: Optional[Committee] = None,
    ) -> AcceptReport:
        return self.apply_batch([(header, certificate, next_committee)])

    # ------------------------------------------------------------------ #
    # Offline replay: ordered contiguous batch                            #
    # ------------------------------------------------------------------ #
    def apply_batch(
        self,
        items: List[tuple],
    ) -> AcceptReport:
        """Apply an ordered batch of ``(Header, Certificate, Committee|None)``.

        Atomic: every item must be accepted or none are and the tip is
        unchanged. Freshness is evaluated once, against the pre-batch tip and
        the last header's timestamp — a contiguous replay that ends on a fresh
        tip is allowed even if intermediate headers are old.
        """
        run_id = self.recorder.run_id
        report = AcceptReport(accepted=False, run_id=run_id)

        # Resource limit first: oversized batch -> RESOURCE.
        if len(items) > self.config.max_batch_size:
            raise LightClientError(
                Code.BATCH_TOO_LARGE,
                f"batch size {len(items)} exceeds limit {self.config.max_batch_size}",
                details={"size": len(items), "limit": self.config.max_batch_size},
            )
        if not items:
            raise LightClientError(
                Code.MALFORMED_HEADER, "update batch must contain at least one header"
            )

        before = self.store.get_tip()
        report.tip_before = _tip_dict(before)
        report.active_committee_before = (
            None
            if before.active_committee_commitment is None
            else "0x" + before.active_committee_commitment.hex()
        )

        try:
            if before.tip_header_root is None:
                raise LightClientError(
                    Code.NOT_INITIALIZED,
                    "no trusted checkpoint installed; cannot update from a peer",
                )

            # Internal batch duplicate detection (same root twice in one batch).
            seen_roots: set = set()
            for i, item in enumerate(items):
                h = item[0]
                root = encoding.header_root(h)
                if root in seen_roots:
                    raise LightClientError(
                        Code.BATCH_DUPLICATE_HEADER,
                        f"header at batch index {i} duplicates an earlier batch item",
                        details={"index": i, "root": "0x" + root.hex()},
                    )
                seen_roots.add(root)

            # Freshness gate. A long-offline client whose tip is past the trust
            # period must get a new out-of-band checkpoint — it will not accept a
            # peer-supplied chain, even a contiguous one.
            now = self.clock.now_ms()
            tip_age = now - before.tip_timestamp_ms
            report.trace.append(
                f"freshness gate: tip age {tip_age}ms / trust period "
                f"{self.config.trust_period_ms}ms"
            )
            if tip_age > self.config.trust_period_ms:
                last_h = items[-1][0]
                self._reject_trust_expired(
                    report, before, tip_age, len(items), last_h, boundary="tip_old"
                )

            # Also bound the batch: its final (newest) header must be within the
            # trust period of now. Replaying only historical headers cannot
            # re-freshen a stale client.
            last_header = items[-1][0]
            newest_age = now - last_header.timestamp_ms
            if newest_age > self.config.trust_period_ms:
                self._reject_trust_expired(
                    report, before, tip_age, len(items), last_header,
                    boundary="newest_header_old",
                )

            with self.store.chain_txn() as conn:
                current_tip_root = before.tip_header_root
                current_committee_commitment = before.active_committee_commitment
                current_tip_round = before.tip_round
                current_tip_ts = before.tip_timestamp_ms

                for index, (header, certificate, next_committee) in enumerate(items):
                    root = encoding.header_root(header)
                    report.root = "0x" + root.hex()
                    report.round = header.round
                    report.parent_root = "0x" + header.parent_root.hex()
                    trace = report.trace
                    trace.append(
                        f"[{index}] verify round={header.round} "
                        f"root=0x{root.hex()[:12]}…"
                    )

                    # Idempotency first: replaying the exact current tip is a
                    # no-op success of delivery, reported as ALREADY_KNOWN, not
                    # as a conflicting branch (its parent is an older header).
                    if root == current_tip_root:
                        raise LightClientError(
                            Code.ALREADY_KNOWN,
                            "header is already the trusted tip",
                            details={"root": "0x" + root.hex()},
                            trace=trace,
                        )

                    # Rule 3: parent link.
                    if header.parent_root != current_tip_root:
                        if self.store.has_header(header.parent_root, conn):
                            raise LightClientError(
                                Code.CONFLICT_EQUIVOCATION,
                                f"header parent 0x{header.parent_root.hex()[:12]}… "
                                "is a known non-tip header: refusing a conflicting "
                                "branch (equivocation)",
                                details={
                                    "index": index,
                                    "parent_root": "0x" + header.parent_root.hex(),
                                    "tip_root": "0x" + current_tip_root.hex(),
                                },
                                trace=trace,
                            )
                        if header.parent_root == root:
                            # self-parent
                            raise LightClientError(
                                Code.UNTRUSTED_BRANCH,
                                "header names itself as parent",
                                details={"index": index, "root": "0x" + root.hex()},
                                trace=trace,
                            )
                        raise LightClientError(
                            Code.UNTRUSTED_BRANCH,
                            f"parent 0x{header.parent_root.hex()[:12]}… is not the "
                            "trusted tip and is unknown: cannot update root from an "
                            "untrusted branch",
                            details={
                                "index": index,
                                "parent_root": "0x" + header.parent_root.hex(),
                                "tip_root": "0x" + current_tip_root.hex(),
                            },
                            trace=trace,
                        )

                    # Rule 4: round strictly increases.
                    if header.round < current_tip_round:
                        raise LightClientError(
                            Code.STALE_ROUND,
                            f"round {header.round} <= current tip round "
                            f"{current_tip_round}",
                            details={
                                "index": index,
                                "round": header.round,
                                "tip_round": current_tip_round,
                            },
                            trace=trace,
                        )
                    if header.round == current_tip_round:
                        raise LightClientError(
                            Code.CONFLICT_EQUIVOCATION,
                            f"a different header at the same round "
                            f"{header.round} conflicts with the tip",
                            details={
                                "index": index,
                                "round": header.round,
                                "tip_root": "0x" + current_tip_root.hex(),
                            },
                            trace=trace,
                        )

                    # Rule 5: timestamps.
                    if header.timestamp_ms < current_tip_ts:
                        raise LightClientError(
                            Code.HEADER_BACKDATED,
                            f"timestamp {header.timestamp_ms} is before parent "
                            f"timestamp {current_tip_ts}",
                            details={
                                "index": index,
                                "timestamp": header.timestamp_ms,
                                "parent_timestamp": current_tip_ts,
                            },
                            trace=trace,
                        )
                    skew_future = header.timestamp_ms - now
                    if skew_future > self.config.future_skew_ms:
                        raise LightClientError(
                            Code.HEADER_FUTURE,
                            f"header timestamp {header.timestamp_ms} is "
                            f"{skew_future}ms in the future (skew allowance "
                            f"{self.config.future_skew_ms}ms)",
                            details={
                                "index": index,
                                "timestamp": header.timestamp_ms,
                                "now_ms": now,
                                "skew_ms": skew_future,
                            },
                            trace=trace,
                        )

                    # Rule 6: cert/root binding + committee resolution.
                    if certificate.header_root != root:
                        raise LightClientError(
                            Code.CERT_BIND_MISMATCH,
                            "certificate is not bound to this header's root",
                            details={
                                "index": index,
                                "cert_root": "0x" + certificate.header_root.hex(),
                                "header_root": "0x" + root.hex(),
                            },
                            trace=trace,
                        )

                    authorizing = self.store.get_committee(
                        current_committee_commitment, conn
                    )
                    if authorizing is None:  # impossible if storage consistent
                        raise LightClientError(
                            Code.INTERNAL,
                            "active committee missing from store",
                            trace=trace,
                        )

                    # Rotation payload binding.
                    new_commitment: Optional[bytes] = None
                    if header.next_committee_commitment is not None:
                        if next_committee is None:
                            raise LightClientError(
                                Code.ROTATION_MISSING_COMMITTEE,
                                "header announces a next committee commitment "
                                "but no committee was supplied",
                                details={
                                    "index": index,
                                    "declared": "0x" + header.next_committee_commitment.hex(),
                                },
                                trace=trace,
                            )
                        provided_commitment = encoding.committee_commitment(
                            next_committee
                        )
                        if provided_commitment != header.next_committee_commitment:
                            raise LightClientError(
                                Code.ROTATION_COMMITMENT_MISMATCH,
                                "supplied committee does not hash to the "
                                "commitment announced in the header",
                                details={
                                    "index": index,
                                    "declared": "0x" + header.next_committee_commitment.hex(),
                                    "provided": "0x" + provided_commitment.hex(),
                                },
                                trace=trace,
                            )
                        new_commitment = provided_commitment
                        trace.append(
                            f"[{index}] rotation to committee "
                            f"0x{new_commitment.hex()[:12]}… authorized by "
                            "prior committee"
                        )

                    # Rules 7-9: membership, signatures, weight threshold.
                    cert_result = verify_certificate(certificate, authorizing, root)
                    report.cert = {
                        "total_weight": cert_result.total_weight,
                        "required_weight": cert_result.required_weight,
                        "signed_weight": cert_result.signed_weight,
                        "distinct_signers": cert_result.distinct_signers,
                    }
                    trace.append(
                        f"[{index}] cert: {cert_result.signed_weight}/"
                        f"{cert_result.total_weight} weight, need "
                        f"{cert_result.required_weight}; "
                        f"{cert_result.distinct_signers} distinct signers"
                    )
                    if not cert_result.verified:
                        raise LightClientError(
                            Code.INSUFFICIENT_WEIGHT,
                            cert_result.failure_detail
                            or "certificate below committee weight threshold",
                            details={
                                "index": index,
                                "signed_weight": cert_result.signed_weight,
                                "required_weight": cert_result.required_weight,
                                "total_weight": cert_result.total_weight,
                            },
                            trace=trace,
                        )

                    # Rule 10: commit.
                    if new_commitment is not None and not self.store.has_committee(
                        new_commitment, conn
                    ):
                        self.store.insert_committee(
                            conn, next_committee, new_commitment
                        )
                    self.store.insert_header(conn, header, root)
                    self.store.set_tip(
                        conn,
                        root,
                        header.round,
                        header.timestamp_ms,
                        new_commitment or current_committee_commitment,
                    )
                    current_tip_root = root
                    current_tip_round = header.round
                    current_tip_ts = header.timestamp_ms
                    if new_commitment is not None:
                        current_committee_commitment = new_commitment
                    trace.append(f"[{index}] ACCEPTED; tip advanced")

                after = self.store.get_tip()
        except LightClientError as exc:
            # Transaction was rolled back by the context manager. Verify and
            # record that trusted state is identical to the snapshot.
            after_exc = self.store.get_tip()
            unchanged = self._same_tip(before, after_exc)
            report.tip_after = _tip_dict(after_exc)
            report.active_committee_after = (
                None
                if after_exc.active_committee_commitment is None
                else "0x" + after_exc.active_committee_commitment.hex()
            )
            report.failure_code = exc.code.value
            report.failure_category = exc.category.value
            report.failure_reason = exc.message
            report.trace.append(
                f"REJECTED {exc.code.value} ({exc.category.value}); "
                f"trusted tip unchanged={unchanged}"
            )
            report.trace.extend(exc.trace[len(report.trace):])
            self._record(
                "update_rejected",
                **report.to_dict(),
                error_details=exc.details,
                state_unchanged=unchanged,
            )
            if not unchanged:
                # A bug in our atomicity guarantees. Fail loudly.
                raise RuntimeError(
                    "trusted tip changed after a rejected update"
                ) from exc
            exc.trace = list(report.trace)
            raise

        report.accepted = True
        report.tip_after = _tip_dict(after)
        report.active_committee_after = (
            None if after.active_committee_commitment is None
            else "0x" + after.active_committee_commitment.hex()
        )
        report.trace.append(
            f"batch of {len(items)} committed; new sequence={after.sequence}"
        )
        self._record("update_accepted", **report.to_dict())
        return report

    # ------------------------------------------------------------------ #
    # Internal helpers                                                    #
    # ------------------------------------------------------------------ #
    def _reject_trust_expired(
        self,
        report: AcceptReport,
        before: TipState,
        tip_age: int,
        batch_len: int,
        last_header: Header,
        *,
        boundary: str,
    ) -> None:
        after = self.store.get_tip()
        report.tip_after = _tip_dict(after)
        report.active_committee_after = (
            None
            if after.active_committee_commitment is None
            else "0x" + after.active_committee_commitment.hex()
        )
        report.failure_code = Code.TRUST_EXPIRED.value
        report.failure_category = "trust_expired"
        report.needs_new_checkpoint = True
        if boundary == "tip_old":
            reason = (
                f"trusted tip is {tip_age}ms old, past the trust period "
                f"{self.config.trust_period_ms}ms; refusing a peer update "
                "after a long offline period — a new out-of-band checkpoint "
                "is required"
            )
        else:
            reason = (
                "replay batch does not end within the trust period; historical "
                "headers alone cannot re-freshen the client — a new checkpoint "
                "is required"
            )
        report.failure_reason = reason
        report.trace.append(
            f"REJECTED TRUST_EXPIRED ({boundary}); trusted tip unchanged; "
            "needs_new_checkpoint=true"
        )
        self._record(
            "update_rejected_trust",
            **report.to_dict(),
            state_unchanged=self._same_tip(before, after),
            boundary=boundary,
            batch_len=batch_len,
            newest_header_ts=last_header.timestamp_ms,
        )
        raise LightClientError(
            Code.TRUST_EXPIRED,
            reason,
            details={
                "tip_age_ms": tip_age,
                "trust_period_ms": self.config.trust_period_ms,
                "boundary": boundary,
                "needs_new_checkpoint": True,
            },
            trace=list(report.trace),
        )

    @staticmethod
    def _same_tip(a: TipState, b: TipState) -> bool:
        return (
            a.tip_header_root == b.tip_header_root
            and a.active_committee_commitment == b.active_committee_commitment
            and a.tip_round == b.tip_round
            and a.tip_timestamp_ms == b.tip_timestamp_ms
        )

    def _record(self, event: str, **fields: Any) -> None:
        self.recorder.record(event, **fields)
