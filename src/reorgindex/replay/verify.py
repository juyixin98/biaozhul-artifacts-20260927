"""Consistency verification: live index vs full rebuild vs expected manifest."""
from __future__ import annotations

from pathlib import Path

from ..storage.store import IndexStore
from .rebuild import rebuild_file


def _derived_signature(store: IndexStore) -> dict:
    return {
        "active_hashes": store.active_hashes(),
        "accounts": store.accounts_snapshot(),
        "events": [
            (
                r["block_hash"],
                r["height"],
                r["txid"],
                r["position_in_block"],
                r["kind"],
                r["address"],
                int(r["amount_delta"]),
                int(r["nonce_delta"]),
            )
            for r in store.all_derived_events()
        ],
    }


def derived_signature_of_db(db_path: str | Path) -> dict:
    store = IndexStore(db_path)
    try:
        return _derived_signature(store)
    finally:
        store.close()


def verify_rebuild_consistency(
    fixture_path: str | Path,
    live_db_path: str | Path,
    rebuilt_db_path: str | Path,
    *,
    allowed_difficulties: set[int],
    finality_depth: int,
    authorized_producers: set[str],
) -> dict:
    """Rebuild a fixture into a second DB and compare both derived projections."""
    live = derived_signature_of_db(live_db_path)
    rebuilt_report = rebuild_file(
        fixture_path,
        rebuilt_db_path,
        allowed_difficulties=allowed_difficulties,
        finality_depth=finality_depth,
        authorized_producers=authorized_producers,
    )
    rebuilt_db = IndexStore(rebuilt_db_path)
    try:
        rebuilt = _derived_signature(rebuilt_db)
    finally:
        rebuilt_db.close()

    consistent = live == rebuilt
    return {
        "consistent": consistent,
        "live_active_tip": live["active_hashes"][-1] if live["active_hashes"] else None,
        "rebuilt_active_tip": rebuilt["active_hashes"][-1] if rebuilt["active_hashes"] else None,
        "event_counts": {"live": len(live["events"]), "rebuilt": len(rebuilt["events"])},
        "report": rebuilt_report,
    }
