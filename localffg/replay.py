"""Offline replay / deterministic re-derivation.

Reads the append-only journal from SQLite, feeds every raw signed envelope
into a FRESH kernel in original seq order, and verifies:

  1. the status of each submission is reproduced exactly (mismatch is a
     deterministic replay failure, with the seq/run id reported);
  2. the re-derived evidence set (by canonical evidence id) equals the
     evidence table contents;
  3. every piece of evidence independently re-checks VALID via the
     standalone checker (cryptographic verification included).

The replay never treats an exception as success: any divergence or checker
rejection flips the verdict to FAIL.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .checker import CheckVerdict, recheck_evidence
from .kernel import SlashingKernel
from .logging_utils import JsonRunLogger
from .models import SignedVote
from .storage import VoteStore


@dataclass
class ReplayReport:
    verdict: str  # "OK" | "FAIL"
    run_id: str
    events_replayed: int = 0
    status_matches: int = 0
    status_mismatches: list[dict] = field(default_factory=list)
    stored_evidence_ids: list[str] = field(default_factory=list)
    rederived_evidence_ids: list[str] = field(default_factory=list)
    evidence_set_match: bool = False
    checker_results: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "run_id": self.run_id,
            "events_replayed": self.events_replayed,
            "status_matches": self.status_matches,
            "status_mismatches": self.status_mismatches,
            "stored_evidence_ids": self.stored_evidence_ids,
            "rederived_evidence_ids": self.rederived_evidence_ids,
            "evidence_set_match": self.evidence_set_match,
            "checker_results": self.checker_results,
            "errors": self.errors,
        }


def replay_store(
    store: VoteStore,
    *,
    logger: JsonRunLogger | None = None,
    run_id: str | None = None,
) -> ReplayReport:
    logger = logger or JsonRunLogger(run_id=run_id, echo=False)
    report = ReplayReport(verdict="OK", run_id=logger.run_id)

    try:
        registry = store.load_registry()
        domain = store.load_domain()
    except Exception as exc:
        report.verdict = "FAIL"
        report.errors.append(f"context load: {type(exc).__name__}: {exc}")
        logger.error("replay_context_failed", error=report.errors[-1])
        return report

    kernel = SlashingKernel(domain=domain, registry=registry)

    events = list(store.iter_events())
    total = len(events)
    logger.info("replay_start", events=total, chain_id=registry.chain_id, epoch_length=registry.epoch_length)

    for i, ev in enumerate(events, start=1):
        report.events_replayed += 1
        try:
            signed = SignedVote.from_json_dict(ev["signed_json"])
            result = kernel.ingest(signed)
        except Exception as exc:
            report.verdict = "FAIL"
            report.errors.append(f"seq {ev['seq']}: {type(exc).__name__}: {exc}")
            logger.error("replay_event_error", seq=ev["seq"], original_run_id=ev["run_id"], error=str(exc))
            continue

        if result.status.value == ev["status"]:
            report.status_matches += 1
            logger.step(
                i,
                total,
                "replay_step",
                seq=ev["seq"],
                original_run_id=ev["run_id"],
                validator=ev["signed_json"].get("validator_id"),
                stored_status=ev["status"],
                replayed_status=result.status.value,
                match=True,
                basis=result.reason,
            )
        else:
            report.verdict = "FAIL"
            mismatch = {
                "seq": ev["seq"],
                "original_run_id": ev["run_id"],
                "stored_status": ev["status"],
                "replayed_status": result.status.value,
                "stored_reason": ev["reason"],
                "replayed_reason": result.reason,
            }
            report.status_mismatches.append(mismatch)
            logger.step(
                i,
                total,
                "replay_status_mismatch",
                **mismatch,
                match=False,
            )

    rederived = sorted(e.evidence_id for e in kernel.evidence())
    stored = sorted(b["evidence_id"] for b in store.list_evidence())
    report.rederived_evidence_ids = rederived
    report.stored_evidence_ids = stored
    report.evidence_set_match = rederived == stored
    if not report.evidence_set_match:
        report.verdict = "FAIL"
        report.errors.append(
            f"evidence set differs: missing_on_replay={sorted(set(stored) - set(rederived))} "
            f"new_on_replay={sorted(set(rederived) - set(stored))}"
        )

    # independent re-verification of every stored bundle
    for bundle in store.list_evidence():
        check = recheck_evidence(bundle, registry, domain=domain)
        d = check.as_dict()
        d["evidence_id"] = bundle["evidence_id"]
        report.checker_results.append(d)
        if check.verdict is not CheckVerdict.VALID:
            report.verdict = "FAIL"
            report.errors.append(
                f"checker {check.verdict.value} for {bundle['evidence_id']}: {check.failures}"
            )
            logger.warn("replay_checker_rejected", evidence_id=bundle["evidence_id"], failures=check.failures)
        else:
            logger.info(
                "replay_checker_valid",
                evidence_id=bundle["evidence_id"],
                kind=check.derived_kind,
                weight=check.derived_weight,
                weight_epoch=check.weight_epoch,
            )

    logger.info(
        "replay_done",
        verdict=report.verdict,
        events=report.events_replayed,
        status_matches=report.status_matches,
        evidence_count=len(stored),
        errors=len(report.errors),
    )
    return report
