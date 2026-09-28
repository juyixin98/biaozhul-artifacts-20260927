"""Offline replay: rebuild the derived index from stored/sealed blocks.

Two entry points:

* :func:`rebuild_file` -- wipe and re-ingest a fixture file's blocks in arrival
  order into a fresh database, returning a structured report (including every
  pending/accept/reject decision and the rollback ranges emitted);
* :func:`verify_store` -- independently rebuild a copy of an existing store and
  compare the derived events/accounts against the live index.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..diag.logger import Diagnostics, new_request_id
from ..kernel.engine import ChainEngine
from ..storage.store import IndexStore


def _silent_diag(store: IndexStore) -> Diagnostics:
    diag = Diagnostics(store, log_level="ERROR")
    return diag


def rebuild_file(
    fixture_path: str | Path,
    db_path: str | Path,
    *,
    allowed_difficulties: set[int],
    finality_depth: int,
    authorized_producers: set[str],
    resume_interrupted: bool = True,
) -> dict:
    """Replay a fixture JSON file into ``db_path`` (which is wiped)."""
    fixture_path = Path(fixture_path)
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # A rebuild must start from an empty database: remove any prior copy and
    # its WAL sidecars so the command is safely repeatable.
    for suffix in ("", "-wal", "-shm"):
        stale = Path(str(db_path) + suffix)
        stale.unlink(missing_ok=True)
    recording = json.loads(fixture_path.read_text(encoding="utf-8"))
    blocks = recording["blocks"]
    arrivals = recording.get("arrival_order")
    if arrivals is None:
        arrivals = [b["name"] for b in blocks]
    by_name = {b["name"]: b["block"] for b in blocks}

    store = IndexStore(db_path)
    diag = _silent_diag(store)
    engine = ChainEngine(
        store,
        diag,
        allowed_difficulties=set(allowed_difficulties),
        finality_depth=finality_depth,
        authorized_producers=set(authorized_producers),
    )
    decisions: list[dict] = []
    rid = new_request_id()
    try:
        for name in arrivals:
            block = by_name[name]
            crash = "after_detach" if name == recording.get("crash_after") else None
            try:
                result = engine.ingest(block, request_id=rid, crash_point=crash)
                entry = {"name": name, **result.as_dict()}
            except Exception as exc:  # SwitchInterrupted injection
                from ..storage.store import SwitchInterrupted

                if isinstance(exc, SwitchInterrupted) and resume_interrupted:
                    engine.resume_switch(request_id=rid)
                    tip = store.active_tip()
                    entry = {
                        "name": name,
                        "outcome": "ACCEPT_SWITCH_RESUMED",
                        "block_hash": tip["hash"],
                        "height": int(tip["height"]),
                        "weight": int(tip["weight"]),
                        "parent": block["parent"],
                        "detail": "interrupted switch completed from durable plan",
                    }
                else:
                    raise
            decisions.append(entry)

        tip = store.active_tip()
        report = {
            "fixture": fixture_path.name,
            "allowed_difficulties": sorted(allowed_difficulties),
            "finality_depth": finality_depth,
            "active_tip": tip["hash"] if tip else None,
            "active_height": int(tip["height"]) if tip else None,
            "active_hashes": store.active_hashes(),
            "pending_count": store.pending_count(),
            "accounts": store.accounts_snapshot(),
            "derived_events": [dict(r) for r in store.all_derived_events()],
            "decisions": decisions,
        }
        return report
    finally:
        store.close()
