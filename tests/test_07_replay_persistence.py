"""Offline replay, durable rebuild, and run/input correlation."""

from __future__ import annotations

import json
from pathlib import Path

from ffg_slash.config import AppConfig
from ffg_slash.detector import SlashingService
from ffg_slash.logging_setup import configure_logging
from ffg_slash.models import IngestStatus
from ffg_slash.replay import replay
from ffg_slash.registry import ValidatorRegistry
from ffg_slash.storage import Storage

from .conftest import CHAIN_ID, GENESIS_ROOT, corrupt_signature, make_vote


def _config(tmp_path: Path, keys) -> AppConfig:
    return AppConfig(
        chain_id=CHAIN_ID, genesis_root=GENESIS_ROOT,
        epochs={
            1: {keys["alpha"][1]: 1, keys["bravo"][1]: 1,
                keys["charlie"][1]: 1, keys["delta"][1]: 1},
            2: {keys["alpha"][1]: 1, keys["bravo"][1]: 1,
                keys["charlie"][1]: 1, keys["delta"][1]: 1},
            3: {keys["alpha"][1]: 1, keys["bravo"][1]: 1,
                keys["charlie"][1]: 1, keys["echo"][1]: 1},
        },
        validator_seeds={},
        database=tmp_path / "replay.sqlite3",
        log_dir=tmp_path / "logs")


def test_replay_jsonl_counts_illegal_and_real_conflicts_separately(tmp_path, keys):
    config = _config(tmp_path, keys)
    sa, pa = keys["alpha"]
    good_a = make_vote(sa, pa, source_epoch=1, target_epoch=2,
                       target_root=b"\xAA" * 32)
    good_b = make_vote(sa, pa, source_epoch=1, target_epoch=2,
                       target_root=b"\xBB" * 32)
    forged = corrupt_signature(
        make_vote(sa, pa, source_epoch=1, target_epoch=2,
                  target_root=b"\xCC" * 32))

    lines = [good_a.to_envelope(), forged.to_envelope(),
             good_a.to_envelope(), good_b.to_envelope()]
    feed = tmp_path / "feed.jsonl"
    feed.write_text("\n".join(json.dumps(x) for x in lines)
                    + "\n# a comment\n{broken json}\n")

    report = replay(config, feed)
    assert report.total == 5  # 4 valid objects + 1 malformed line (comment skipped)
    assert report.accepted == 2
    assert report.duplicate == 1
    assert report.invalid_signatures == 1
    assert report.rejected_by_reason["invalid_signature"] == 1
    assert report.rejected_by_reason["malformed"] == 1
    # one REAL conflict only; forged input did not create evidence
    assert report.real_conflicts_total == 1
    assert len(report.evidences) == 1
    assert report.evidences[0]["type"] == "double_vote"
    assert report.run_id  # correlated identity present


def test_state_rebuilds_from_disk_without_reprocessing(tmp_path, keys):
    config = _config(tmp_path, keys)
    logger, run_id, _ = configure_logging(config.log_dir)
    storage = Storage(config.database)
    storage.init_meta(config.chain_id, config.genesis_root)
    registry = ValidatorRegistry(config.chain_id)
    for epoch, members in config.epochs.items():
        registry.add_epoch(epoch, members)
    svc1 = SlashingService(config.chain_id, config.genesis_root,
                           registry, storage, run_id, logger)
    seed, pub = keys["bravo"]
    svc1.ingest(make_vote(seed, pub, source_epoch=1, target_epoch=2))
    ev_vote = make_vote(seed, pub, source_epoch=1, target_epoch=2,
                        target_root=b"\x55" * 32)
    svc1.ingest(ev_vote)
    storage.close()

    # reopen: evidence/votes/finality state must be present, counters rebuild
    storage2 = Storage(config.database)
    registry2 = ValidatorRegistry(config.chain_id)
    for epoch, members in config.epochs.items():
        registry2.add_epoch(epoch, members)
    logger2, run_id2, _ = configure_logging(config.log_dir)
    svc2 = SlashingService(config.chain_id, config.genesis_root,
                           registry2, storage2, run_id2, logger2)
    assert len(storage2.list_evidences()) == 1
    assert len(svc2._votes[pub]) == 2
    # the historical conflicting vote is still recognized as a duplicate
    assert svc2.ingest(ev_vote).status is IngestStatus.DUPLICATE


def test_log_lines_carry_run_id_and_named_verdicts(tmp_path, keys):
    config = _config(tmp_path, keys)
    logger, run_id, log_dir = configure_logging(config.log_dir)
    storage = Storage(":memory:")
    storage.init_meta(config.chain_id, config.genesis_root)
    registry = ValidatorRegistry(config.chain_id)
    for epoch, members in config.epochs.items():
        registry.add_epoch(epoch, members)
    svc = SlashingService(config.chain_id, config.genesis_root,
                          registry, storage, run_id, logger)
    seed, pub = keys["alpha"]
    svc.ingest(make_vote(seed, pub, source_epoch=1, target_epoch=2))
    svc.ingest(corrupt_signature(
        make_vote(seed, pub, source_epoch=1, target_epoch=2,
                  target_root=b"\x77" * 32)))

    log_files = list(log_dir.glob("run-*.log"))
    assert log_files, "expected a per-run log file"
    text = log_files[0].read_text()
    assert run_id in text
    lines = [json.loads(line) for line in text.splitlines() if line.strip()]
    assert any(l.get("status") == "ok" and l.get("step") == "verify_signature"
               for l in lines)
    rejected = [l for l in lines if l.get("reject_reason") == "invalid_signature"]
    assert rejected and rejected[0]["status"] == "rejected"
    # rejected lines never claim success
    assert all(l.get("status") != "success" for l in lines)
