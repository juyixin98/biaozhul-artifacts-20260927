"""Chain-state kernel: ingest blocks, suspend orphans, choose the best chain.

Fixed fork-choice rule (part of the model, not configuration):

* a block's weight is its declared difficulty (4 for a regular block, 16 for a
  weighted block in the synthetic fixtures; the set is consensus-fixed);
* a chain's cumulative weight is the sum of block weights from genesis to tip;
* the best chain is the one with the greatest cumulative weight; ties are
  broken by the *lower* tip hash (deterministic, never by arrival time).

Finality rule: a block at height ``h`` is final when its depth below the
active tip satisfies ``tip_height - h >= K`` (i.e. it has more than
``K`` confirmations counting itself).  A candidate switch whose divergence
point would require detaching a final block is rejected with
``REORG_FINALIZED``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..diag.logger import Diagnostics
from ..storage.store import IndexStore, SwitchInterrupted
from .derivation import block_ledger_deltas, txids_of
from .errors import IngestionError, Outcome, RejectReason
from .state import ChainState
from .validation import verify_block


@dataclass
class IngestResult:
    outcome: str
    block_hash: str
    height: int
    weight: int
    parent: str
    reason: Optional[str] = None
    detail: Optional[str] = None
    # Present on ACCEPT_SWITCH:
    switch: Optional[dict] = None
    # Blocks released from the orphan table while processing this request.
    released: list[str] = field(default_factory=list)
    # Released orphans that failed stateful validation: {hash, reason}.
    rejected_orphans: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "outcome": self.outcome,
            "block_hash": self.block_hash,
            "height": self.height,
            "weight": self.weight,
            "parent": self.parent,
            "reason": self.reason,
            "detail": self.detail,
            "switch": self.switch,
            "released": self.released,
        }


class ChainEngine:
    def __init__(
        self,
        store: IndexStore,
        diag: Diagnostics,
        *,
        allowed_difficulties: set[int],
        finality_depth: int,
        authorized_producers: set[str],
    ):
        self.store = store
        self.diag = diag
        self.allowed_difficulties = set(allowed_difficulties)
        self.finality_depth = finality_depth
        self.authorized_producers = set(authorized_producers)

    # --------------------------------------------------------- helpers
    def _cumulative_weight(self, tip_hash: str) -> int:
        """Sum of per-block weights from genesis up to ``tip_hash``."""
        total = 0
        cursor: Optional[str] = tip_hash
        while cursor is not None and cursor != "0" * 64:
            meta = self.store.get_block_meta(cursor)
            total += int(meta["weight"])
            cursor = meta["parent"]
        return total

    def active_cumulative_weight(self) -> Optional[int]:
        tip = self.store.active_tip()
        return self._cumulative_weight(tip["hash"]) if tip else None

    def confirmations(self, block_hash: str) -> Optional[int]:
        """Depth including the block itself; None if not on the active chain."""
        meta = self.store.get_block_meta(block_hash)
        if meta is None or not meta["is_active"]:
            return None
        tip = self.store.active_tip()
        if tip is None:
            return None
        return int(tip["height"]) - int(meta["height"]) + 1

    def is_final(self, block_hash: str) -> bool:
        confs = self.confirmations(block_hash)
        return confs is not None and confs > self.finality_depth

    def _chain_hashes_from(self, tip_hash: str) -> list[str]:
        out: list[str] = []
        cursor: Optional[str] = tip_hash
        while cursor is not None and cursor != "0" * 64:
            meta = self.store.get_block_meta(cursor)
            if meta is None:  # pragma: no cover - defensive
                raise IngestionError(
                    RejectReason.PARENT_UNKNOWN,
                    f"missing block {cursor[:12]}… while walking chain",
                )
            out.append(cursor)
            cursor = meta["parent"]
        out.reverse()
        return out

    def _load_state_at(self, block_hash: str) -> ChainState:
        """Replay genesis..block_hash from stored payloads."""
        hashes = self._chain_hashes_from(block_hash)
        state = ChainState()
        for h in hashes:
            state.apply_block(self.store.get_block_payload(h))
        return state

    # --------------------------------------------------------- ingest
    def ingest(self, block: dict, *, request_id: str, crash_point: Optional[str] = None) -> IngestResult:
        """Top-level ingestion entry point (orchestrates orphan release)."""
        result = self._ingest_one(block, request_id=request_id, crash_point=crash_point)
        if result.outcome in (Outcome.ACCEPT_EXTEND.value, Outcome.ACCEPT_SWITCH.value):
            self._release_pending(result.block_hash, request_id, result)
        return result

    def _release_pending(self, parent_hash: str, request_id: str, root: IngestResult) -> None:
        queue = [parent_hash]
        while queue:
            anchor = queue.pop(0)
            for child_hash, child_block, seq in self.store.pop_pending_children(anchor):
                # A pending block was statelessly valid when it arrived; once
                # its parent becomes available it may still fail stateful
                # validation (e.g. replay on the now-active chain).  Isolate
                # each child so one invalid orphan neither aborts the request
                # nor blocks its siblings.
                try:
                    child_result = self._ingest_one(
                        child_block,
                        request_id=request_id,
                        known_hash=child_hash,
                        known_seq=seq,
                    )
                except IngestionError as exc:
                    self._archive_rejected_orphan(
                        child_hash, child_block, seq, exc, request_id
                    )
                    root.rejected_orphans.append(
                        {"block_hash": child_hash, "reason": exc.reason.value}
                    )
                    continue
                root.released.append(child_hash)
                if child_result.outcome in (
                    Outcome.ACCEPT_EXTEND.value,
                    Outcome.ACCEPT_SWITCH.value,
                ):
                    queue.append(child_result.block_hash)
                # fork children simply stop the cascade from that edge; other
                # pending rows are unaffected.

    def _archive_rejected_orphan(
        self,
        block_hash: str,
        block: dict,
        seq: int,
        exc: IngestionError,
        request_id: str,
    ) -> None:
        """Persist a released orphan that failed stateful validation, inactive."""
        self.store.insert_block(
            block_hash=block_hash,
            block=block,
            weight=int(block["difficulty"]),
            is_active=False,
            seq=seq,
        )
        if self.store.get_block_meta(block["parent"]) is not None:
            self.store.insert_fork_locations(
                block_hash, block["height"], txids_of(block)
            )
        self.diag.record(
            request_id=request_id,
            outcome=Outcome.REJECTED.value,
            reason=exc.reason.value,
            block_hash=block_hash,
            height=block["height"],
            parent=block["parent"],
            weight=int(block["difficulty"]),
            detail=f"released orphan rejected: {exc.detail}",
        )

    def _ingest_one(
        self,
        block: dict,
        *,
        request_id: str,
        known_hash: Optional[str] = None,
        known_seq: Optional[int] = None,
        crash_point: Optional[str] = None,
    ) -> IngestResult:
        # 1) stateless verification (also yields the identity hash).
        identity = verify_block(
            block,
            allowed_difficulties=self.allowed_difficulties,
            authorized_producers=self.authorized_producers,
        )
        if known_hash is not None and known_hash != identity:  # pragma: no cover
            raise IngestionError(
                RejectReason.MALFORMED,
                "orphan block re-derived to a different hash",
            )

        height = block["height"]
        block_weight = int(block["difficulty"])
        parent = block["parent"]

        # 2) duplicate by hash
        if self.store.has_block(identity):
            result = IngestResult(
                outcome=Outcome.DUPLICATE.value,
                block_hash=identity,
                height=height,
                weight=block_weight,
                parent=parent,
                reason=RejectReason.ALREADY_KNOWN.value,
                detail="block hash already present",
            )
            self.diag.record(
                request_id=request_id,
                outcome=result.outcome,
                reason=result.reason,
                block_hash=identity,
                height=height,
                parent=parent,
                weight=block_weight,
                detail=result.detail,
            )
            return result

        # 3) genesis vs parent linkage
        tip = self.store.active_tip()
        if height == 0:
            if tip is not None:
                raise IngestionError(
                    RejectReason.SECOND_GENESIS,
                    "a genesis block already exists",
                )
            # The zero-parent rule is checked in verify_block.
            return self._accept_genesis(block, identity, block_weight, request_id)

        if not self.store.has_block(parent):
            seq = self.store.put_pending(identity, block, known_seq)
            result = IngestResult(
                outcome=Outcome.PENDING.value,
                block_hash=identity,
                height=height,
                weight=block_weight,
                parent=parent,
                detail=f"suspended: parent {parent[:12]}… unknown (seq {seq})",
            )
            self.diag.record(
                request_id=request_id,
                outcome=result.outcome,
                block_hash=identity,
                height=height,
                parent=parent,
                weight=block_weight,
                detail=result.detail,
            )
            return result

        # 4) parent known: height must be parent height + 1
        parent_meta = self.store.get_block_meta(parent)
        if height != int(parent_meta["height"]) + 1:
            raise IngestionError(
                RejectReason.HEIGHT_MISMATCH,
                f"height {height} does not extend parent height {parent_meta['height']}",
                parent_height=int(parent_meta["height"]),
            )

        # 5) validate the candidate chain by replaying from the divergence
        candidate_hashes = self._chain_hashes_from(parent)
        state = self._load_state_at(parent)
        state.apply_block(block)  # raises on replay/nonce/balance/etc.

        # 6) fork-choice
        if tip is None:
            # Non-genesis block whose parent is known but no active tip: the
            # only way is that the parent itself is genesis; extend.
            return self._accept_extension(block, identity, block_weight, request_id)

        if parent == tip["hash"]:
            return self._accept_extension(block, identity, block_weight, request_id)

        return self._consider_switch(
            block,
            identity,
            block_weight,
            candidate_hashes=candidate_hashes + [identity],
            request_id=request_id,
            crash_point=crash_point,
        )

    # --------------------------------------------------------- accepts
    def _accept_genesis(
        self, block: dict, identity: str, weight: int, request_id: str
    ) -> IngestResult:
        deltas = block_ledger_deltas(block)
        self.store.insert_block(
            block_hash=identity, block=block, weight=weight, is_active=True
        )
        self.store.apply_extension(
            block_hash=identity, height=0, deltas=deltas, weight=weight
        )
        result = IngestResult(
            outcome=Outcome.ACCEPT_EXTEND.value,
            block_hash=identity,
            height=0,
            weight=weight,
            parent=block["parent"],
            detail="genesis accepted",
        )
        self.diag.record(
            request_id=request_id,
            outcome=result.outcome,
            block_hash=identity,
            height=0,
            parent=block["parent"],
            weight=weight,
            detail=result.detail,
        )
        return result

    def _accept_extension(
        self, block: dict, identity: str, weight: int, request_id: str
    ) -> IngestResult:
        deltas = block_ledger_deltas(block)
        self.store.insert_block(
            block_hash=identity, block=block, weight=weight, is_active=True
        )
        self.store.apply_extension(
            block_hash=identity, height=block["height"], deltas=deltas, weight=weight
        )
        result = IngestResult(
            outcome=Outcome.ACCEPT_EXTEND.value,
            block_hash=identity,
            height=block["height"],
            weight=weight,
            parent=block["parent"],
            detail="extended active chain",
        )
        self.diag.record(
            request_id=request_id,
            outcome=result.outcome,
            block_hash=identity,
            height=block["height"],
            parent=block["parent"],
            weight=weight,
            detail=result.detail,
        )
        return result

    def _consider_switch(
        self,
        block: dict,
        identity: str,
        block_weight: int,
        *,
        candidate_hashes: list[str],
        request_id: str,
        crash_point: Optional[str],
    ) -> IngestResult:
        tip = self.store.active_tip()
        active_hashes = self._chain_hashes_from(tip["hash"])
        candidate_set = set(candidate_hashes)

        # Divergence point = last common ancestor on the active chain.
        divergence_index = -1
        for i, h in enumerate(active_hashes):
            if h in candidate_set:
                divergence_index = i
        if divergence_index < 0:  # pragma: no cover - genesis is always shared
            raise IngestionError(
                RejectReason.MALFORMED, "candidate chain shares no ancestor with active chain"
            )
        detach = active_hashes[divergence_index + 1:]
        attach_hashes = [h for h in candidate_hashes if h not in set(active_hashes)]

        # Cumulative weights along each chain; the candidate parent chain is
        # stored, so its weight is the parent's cumulative weight plus the new
        # block's own weight.
        candidate_weight = self._cumulative_weight(block["parent"]) + block_weight
        active_weight = self._cumulative_weight(tip["hash"])
        wins = candidate_weight > active_weight or (
            candidate_weight == active_weight and identity < tip["hash"]
        )

        if not wins:
            # Valid fork, insufficient weight: store in-hand block inertly.
            self.store.insert_block(
                block_hash=identity, block=block, weight=block_weight, is_active=False
            )
            self.store.insert_fork_locations(identity, block["height"], txids_of(block))
            result = IngestResult(
                outcome=Outcome.ACCEPT_FORK.value,
                block_hash=identity,
                height=block["height"],
                weight=block_weight,
                parent=block["parent"],
                detail=(
                    f"fork stored but not active (candidate weight {candidate_weight} "
                    f"<= active weight {active_weight})"
                ),
            )
            self.diag.record(
                request_id=request_id,
                outcome=result.outcome,
                block_hash=identity,
                height=block["height"],
                parent=block["parent"],
                weight=block_weight,
                detail=result.detail,
            )
            return result

        # Finality boundary: detaching a final block is forbidden.
        if detach:
            first_detach_height = self.store.get_block_meta(detach[0])["height"]
            # Depth of the first block to detach relative to the OLD tip:
            # old_tip_height - first_detach_height + 1 blocks sit on top of it.
            old_tip_height = int(tip["height"])
            confirmations_detached = old_tip_height - first_detach_height + 1
            # A block is final when confirmations > K.  The shallowest block
            # being removed carries the *smallest* confirmation count among
            # detach blocks, so check it.
            if confirmations_detached > self.finality_depth:
                # Store the inert fork for audit, then reject the switch.
                self.store.insert_block(
                    block_hash=identity, block=block, weight=block_weight, is_active=False
                )
                self.store.insert_fork_locations(
                    identity, block["height"], txids_of(block)
                )
                result = IngestResult(
                    outcome=Outcome.REJECTED.value,
                    block_hash=identity,
                    height=block["height"],
                    weight=block_weight,
                    parent=block["parent"],
                    reason=RejectReason.REORG_FINALIZED.value,
                    detail=(
                        f"reorg would roll back {len(detach)} block(s); the shallowest "
                        f"has {confirmations_detached} confirmations > finality_depth "
                        f"{self.finality_depth}"
                    ),
                    switch={
                        "would_detach": detach,
                        "would_attach": attach_hashes,
                        "rollback_heights": [first_detach_height, old_tip_height],
                    },
                )
                self.diag.record(
                    request_id=request_id,
                    outcome=result.outcome,
                    reason=result.reason,
                    block_hash=identity,
                    height=block["height"],
                    parent=block["parent"],
                    weight=block_weight,
                    detail=result.detail,
                    extra={"rollback": result.switch},
                )
                return result

        # ---- Switch accepted: store new fork block(s), then detach/attach.
        self.store.insert_block(
            block_hash=identity, block=block, weight=block_weight, is_active=False
        )
        self.store.insert_fork_locations(identity, block["height"], txids_of(block))

        attach_meta = []
        deltas_by_hash: dict[str, list[dict]] = {}
        for h in attach_hashes:
            payload = self.store.get_block_payload(h)
            meta = self.store.get_block_meta(h)
            attach_meta.append(
                {"hash": h, "height": int(meta["height"]), "weight": int(meta["weight"])}
            )
            deltas_by_hash[h] = block_ledger_deltas(payload)

        # Mark the fork-side tx locations now (on_active flips during attach).
        try:
            self.store.apply_switch(
                detach=detach,
                attach=attach_meta,
                deltas_by_hash=deltas_by_hash,
                request_id=request_id,
                new_tip=identity,
                crash_point=crash_point,
            )
        except SwitchInterrupted:
            # Re-raised for the interruption test; the durable DETACHED plan
            # allows resume_switch() to finish.  Record it, then propagate.
            self.diag.record(
                request_id=request_id,
                outcome="SWITCH_INTERRUPTED",
                reason="CRASH_INJECTION",
                block_hash=identity,
                height=block["height"],
                parent=block["parent"],
                weight=block_weight,
                detail="switch interrupted after durable detach; resume required",
                extra={"detached": detach, "to_attach": attach_hashes},
            )
            raise

        rollback = {
            "detached": detach,
            "attached": attach_hashes,
            "rollback_height_range": [
                self.store.get_block_meta(detach[0])["height"],
                int(tip["height"]),
            ]
            if detach
            else None,
        }
        result = IngestResult(
            outcome=Outcome.ACCEPT_SWITCH.value,
            block_hash=identity,
            height=block["height"],
            weight=block_weight,
            parent=block["parent"],
            detail=(
                f"switch complete: detached {len(detach)}, attached {len(attach_hashes)}"
            ),
            switch=rollback,
        )
        self.diag.record(
            request_id=request_id,
            outcome=result.outcome,
            block_hash=identity,
            height=block["height"],
            parent=block["parent"],
            weight=block_weight,
            detail=result.detail,
            extra={"rollback": rollback},
        )
        return result

    # --------------------------------------------------------- recovery
    def resume_pending(self, request_id: str) -> list[dict]:
        """Re-attempt orphan ingestion after an explicit restart/request.

        Pending blocks whose parent block is known are re-fed one at a time;
        a successful extension/switch cascades via the same release path as
        live ingestion.  A statefully-invalid pending block is archived
        inactive with a diagnostic rather than aborting the resume.
        """
        accepted: list[dict] = []
        progressed = True
        while progressed:
            progressed = False
            for child_hash, child_block, seq in self.store.all_pending():
                # Re-feed only when the complete ancestry is stored AND the
                # parent is on the active chain (a switch release handles the
                # rest); otherwise the orphan stays suspended.
                parent_meta = self.store.get_block_meta(child_block["parent"])
                if parent_meta is None or not parent_meta["is_active"]:
                    continue
                # Remove this one row; siblings stay queued.
                self.store.delete_pending(child_hash)
                progressed = True
                try:
                    result = self._ingest_one(
                        child_block,
                        request_id=request_id,
                        known_hash=child_hash,
                        known_seq=seq,
                    )
                except IngestionError as exc:
                    self._archive_rejected_orphan(
                        child_hash, child_block, seq, exc, request_id
                    )
                    continue
                if result.outcome in (
                    Outcome.ACCEPT_EXTEND.value,
                    Outcome.ACCEPT_SWITCH.value,
                ):
                    accepted.append(result.as_dict())
                    self._release_pending(result.block_hash, request_id, result)
                break  # rescan: pending set changed
        return accepted

    def resume_switch(self, request_id: Optional[str] = None) -> Optional[dict]:
        """Complete any persisted DETACHED switch plan after a restart."""
        plan = self.store.get_plan()
        if plan is None:
            return None
        import json

        attach_hashes = json.loads(plan["attach_hashes_json"])
        deltas_by_hash = {}
        for h in attach_hashes:
            payload = self.store.get_block_payload(h)
            deltas_by_hash[h] = block_ledger_deltas(payload)
        resumed = self.store.resume_switch(deltas_by_hash)
        self.diag.record(
            request_id=request_id or plan["request_id"],
            outcome="SWITCH_RESUMED",
            block_hash=plan["new_tip"],
            detail=f"resumed interrupted switch; attached {len(attach_hashes)} block(s)",
            extra=resumed or {},
        )
        return resumed
