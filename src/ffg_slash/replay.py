"""Offline replay: feed a JSONL/JSON file of envelopes through the detector."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .config import AppConfig
from .detector import SlashingService
from .registry import ValidatorRegistry
from .storage import Storage


@dataclass
class ReplayReport:
    run_id: str
    total: int
    accepted: int
    duplicate: int
    rejected_by_reason: dict
    invalid_signatures: int
    evidences: list[dict]
    real_conflicts_total: int
    stats: dict

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "total": self.total,
            "accepted": self.accepted,
            "duplicate": self.duplicate,
            "rejected_by_reason": self.rejected_by_reason,
            "invalid_signatures": self.invalid_signatures,
            "real_conflicts_total": self.real_conflicts_total,
            "evidence_count": len(self.evidences),
            "evidences": self.evidences,
            "stats": self.stats,
        }


@dataclass
class MalformedLine:
    """A JSONL line that did not parse; fed to the pipeline as a rejection."""

    lineno: int
    text: str


def iter_inputs(raw: str):
    """Yield envelope objects from JSONL, a JSON array, or one JSON object.

    Unparseable JSONL lines are yielded as :class:`MalformedLine` so the
    detector records them as ``malformed`` rejections instead of aborting.
    """
    stripped = raw.strip()
    if not stripped:
        return
    if stripped[0] in "[{":
        try:
            data = json.loads(stripped)
        except ValueError:
            data = None
        if data is not None:
            if isinstance(data, list):
                for item in data:
                    yield item
                return
            if isinstance(data, dict) and isinstance(data.get("votes"), list):
                for item in data["votes"]:
                    yield item
                return
            if isinstance(data, dict):
                yield data
                return
    for lineno, line in enumerate(stripped.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            yield json.loads(line)
        except ValueError as exc:
            yield MalformedLine(lineno, f"{line} ({exc})")


def build_service(config: AppConfig, storage: Storage, run_id: str,
                  logger=None) -> SlashingService:
    storage.init_meta(config.chain_id, config.genesis_root)
    registry = ValidatorRegistry(config.chain_id)
    existing = storage.load_epochs()
    for epoch, members in existing.items():
        registry.add_epoch(epoch, members)
    for epoch, members in config.epochs.items():
        registry.add_epoch(epoch, members)
        storage.upsert_epoch(epoch, members)
    return SlashingService(
        chain_id=config.chain_id, genesis_root=config.genesis_root,
        registry=registry, storage=storage, run_id=run_id, logger=logger)


def replay(config: AppConfig, feed_path: str | Path,
           db_path: str | Path | None = None, run_id: str | None = None) -> ReplayReport:
    from .logging_setup import configure_logging
    logger, auto_run_id, _ = configure_logging(config.log_dir)
    run_id = run_id or auto_run_id
    storage = Storage(db_path or config.database)
    service = build_service(config, storage, run_id, logger=logger)

    text = Path(feed_path).read_text(encoding="utf-8")
    total = 0
    try:
        for idx, item in enumerate(iter_inputs(text), 1):
            total += 1
            if isinstance(item, MalformedLine):
                result = service.ingest_raw(item.text)
                label = "malformed-line"
            else:
                result = service.ingest_raw(item)
                label = (item.get("validator_pubkey", "?")[:12]
                         if isinstance(item, dict) else "?")
            logger.info(
                f"replay {idx}: {result.status.value} validator={label}",
                extra={"run_id": run_id, "seq": idx, "step": "replay",
                       "status": result.status.value,
                       "reject_reason": result.reject_reason.value
                       if result.reject_reason else None,
                       "detail": f"input #{idx}", "version": "1.0.0"})
    finally:
        stats = service.stats()
        evidences = storage.list_evidences()
        storage.close()

    return ReplayReport(
        run_id=run_id, total=total,
        accepted=stats["accepted"], duplicate=stats["duplicate"],
        rejected_by_reason=stats["rejected_by_reason"],
        invalid_signatures=stats["invalid_signatures"],
        evidences=evidences, real_conflicts_total=stats["real_conflicts_total"],
        stats=stats)
