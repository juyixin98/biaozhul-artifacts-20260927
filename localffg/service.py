"""Application service: orchestrates kernel + SQLite store + run logging.

Keeps HTTP independent of core logic: given a submitted envelope it returns
the kernel's exact classification and persists every attempt (including
invalid ones) to the journal.
"""
from __future__ import annotations

from dataclasses import dataclass

from .checker import recheck_evidence
from .config import AppConfig
from .crypto import Signer
from .epochs import ValidatorRegistry
from .kernel import IngestResult, SlashingKernel
from .logging_utils import JsonRunLogger, new_run_id
from .models import SignedVote, VoteStatus
from .replay import replay_store
from .storage import VoteStore


@dataclass
class SubmitOutcome:
    seq: int
    status: VoteStatus
    reason: str
    slashable: bool
    evidence: list[dict]
    run_id: str


class VoteService:
    def __init__(self, config: AppConfig, *, logger: JsonRunLogger | None = None, store: VoteStore | None = None):
        self.config = config
        self.logger = logger or JsonRunLogger(echo=True, level=config.log_level)
        self.store = store or VoteStore(config.db_path)
        self.store.init_meta(
            chain_id=config.chain_id,
            epoch_length=config.epoch_length,
            domain=config.domain,
        )
        self.registry = self._load_or_new()
        self.kernel = SlashingKernel(domain=config.domain, registry=self.registry)
        self._prime_kernel_from_journal()

    def _load_or_new(self) -> ValidatorRegistry:
        try:
            return self.store.load_registry()
        except Exception:
            return ValidatorRegistry(chain_id=self.config.chain_id, epoch_length=self.config.epoch_length)

    def _prime_kernel_from_journal(self) -> None:
        """Rebuild in-memory voting state from valid stored envelopes."""
        for ev in self.store.iter_events():
            if ev["status"] in {
                VoteStatus.ACCEPTED.value,
                VoteStatus.DUPLICATE_RETRANSMIT.value,
                VoteStatus.DOUBLE_VOTE.value,
                VoteStatus.SURROUND_VOTE.value,
            }:
                try:
                    signed = SignedVote.from_json_dict(ev["signed_json"])
                    # replay without journaling; kernel state only
                    self.kernel.ingest(signed)
                except Exception:
                    # malformed stored rows cannot rebuild state; logged, not ignored
                    self.logger.error("prime_skip_malformed", seq=ev["seq"])

    # -- registry bootstrap ---------------------------------------------- #

    def register_validator(self, validator_id: str, signer: Signer, weight: int, effective_epoch: int = 0) -> None:
        self.registry.add_validator(validator_id, signer.public_key_bytes, weight, effective_epoch)
        self.registry.set_weight(validator_id, effective_epoch, weight)
        self.store.save_registry(self.registry)
        self.logger.info(
            "validator_registered",
            validator_id=validator_id,
            weight=weight,
            effective_epoch=effective_epoch,
        )

    def set_weight(self, validator_id: str, effective_epoch: int, weight: int) -> None:
        self.registry.set_weight(validator_id, effective_epoch, weight)
        self.store.save_registry(self.registry)
        self.logger.info(
            "weight_updated",
            validator_id=validator_id,
            effective_epoch=effective_epoch,
            weight=weight,
        )

    def install_registry(self, registry: ValidatorRegistry) -> None:
        if registry.chain_id != self.config.chain_id or registry.epoch_length != self.config.epoch_length:
            raise ValueError("registry chain/epoch schedule does not match service config")
        self.registry = registry
        self.store.save_registry(registry)
        self.kernel = SlashingKernel(domain=self.config.domain, registry=registry)
        self._prime_kernel_from_journal()

    # -- operations ------------------------------------------------------- #

    def submit(self, signed: SignedVote, *, run_id: str | None = None) -> SubmitOutcome:
        run_id = run_id or self.logger.run_id
        result: IngestResult = self.kernel.ingest(signed)
        seq = self.store.record_ingest(run_id=run_id, signed=signed, result=result)

        self.logger.info(
            "vote_ingested",
            seq=seq,
            run_id=run_id,
            status=result.status.value,
            reason=result.reason,
            validator=signed.vote.validator_id,
            source=signed.vote.source_round,
            target=signed.vote.target_round,
            slashable=result.slashable,
            evidence_ids=[e.evidence_id for e in result.evidence],
        )
        return SubmitOutcome(
            seq=seq,
            status=result.status,
            reason=result.reason,
            slashable=result.slashable,
            evidence=[e.to_json_dict() for e in result.evidence],
            run_id=run_id,
        )

    def list_evidence(self) -> list[dict]:
        return self.store.list_evidence()

    def get_evidence(self, evidence_id_: str) -> dict | None:
        return self.store.get_evidence(evidence_id_)

    def check_evidence(self, evidence_id_: str) -> dict | None:
        bundle = self.store.get_evidence(evidence_id_)
        if bundle is None:
            return None
        report = recheck_evidence(bundle, self.registry, domain=self.config.domain)
        return report.as_dict()

    def stats(self) -> dict:
        counts = self.store.status_counts()
        return {
            "chain_id": self.config.chain_id,
            "kernel_in_memory": self.kernel.stats.as_dict(),
            "journal": counts,
            "evidence_count": self.store.evidence_count(),
            "validators": sorted(self.registry.snapshot_at_epoch(0).keys()),
        }

    def replay(self) -> dict:
        report = replay_store(self.store, run_id=new_run_id("replay"))
        return report.as_dict()

    def close(self) -> None:
        self.store.close()
